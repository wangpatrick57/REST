from itertools import islice
import os
import sys

from ngram_datastore.ngram_datastore_settings import NGramDatastoreSettings
from ngram_datastore.utils import get_filtered_ngrams
sys.path.append("../")
import torch
from contextlib import contextmanager
import numpy as np
from rest.model.rest_model import RestModel
from rest.model.kv_cache import *
from rest.model.utils import *
import draftretriever

from tqdm import tqdm
import time
import argparse

from human_eval.dataset import HumanEvalDataset

def run_eval(model, tokenizer, datastore, max_token_span, num_draft, temperature, top_p, max_new_token, ngram_datastore_settings: NGramDatastoreSettings, accept_length_fpath, num_benchmark_convs):
    accept_lengths_tree_average = []
    avg_time_per_token_list = []

    accept_lengths_tree_average_micro = []
    avg_time_per_token_list_micro = []
    token_spans = list(range(1, max_token_span + 1))[::-1]
    print("token_spans: ", token_spans)
    filtered_ngrams = get_filtered_ngrams(ngram_datastore_settings)
    dataset_it = dataset if num_benchmark_convs == 0 else islice(dataset, num_benchmark_convs)

    for sample in tqdm(dataset_it, total=len(dataset_it) if num_benchmark_convs == 0 else num_benchmark_convs, desc="virtual_rest_test.run_eval.0"):
        prompt = sample['prompt']

        accept_lengths_tree = []
        with torch.inference_mode():

            # Initialize the past key and value states
            if hasattr(model, "past_key_values"):
                past_key_values = model.past_key_values
                past_key_values_data = model.past_key_values_data
                current_length_data = model.current_length_data
                # Reset the past key and value states
                current_length_data.zero_()
            else:
                (
                    past_key_values,
                    past_key_values_data,
                    current_length_data,
                ) = initialize_past_key_values(model.base_model)
                model.past_key_values = past_key_values
                model.past_key_values_data = past_key_values_data
                model.current_length_data = current_length_data


            new_token = 0
            input_ids = tokenizer([prompt]).input_ids
            input_len = len(input_ids[0])
            input_ids = torch.as_tensor(input_ids).cuda()
            model.base_model.model.draft_mask = None
            logits = initialize_logits(
                    input_ids, model, past_key_values
            )
            cur_length = input_len + 1
            accept_lengths_tree.append(1)
            
            torch.cuda.synchronize()
            start_time = time.time()
            for i in range(2000):
                candidates, tree_candidates, draft_buffers = virtual_generate_candidates_and_draft_buffer(
                        logits,
                        input_ids,
                        datastore,
                        token_spans,
                        filtered_ngrams,
                        top_p,
                        temperature,
                        max_num_draft=num_draft,
                        device=model.base_model.device
                    )
                
                model.base_model.model.draft_mask = draft_buffers["draft_attn_mask"]

                logits, outputs = tree_decoding(
                        model,
                        tree_candidates,
                        past_key_values,
                        draft_buffers["draft_position_ids"],
                        input_ids,
                        draft_buffers["retrieve_indices"],
                    )

                best_candidate, accept_length = evaluate_posterior(
                        logits, candidates, temperature = temperature, top_p=top_p
                    )
                input_ids, logits, new_token = update_inference_inputs(
                        input_ids,
                        candidates,
                        best_candidate,
                        accept_length,
                        draft_buffers["retrieve_indices"],
                        outputs,
                        logits,
                        new_token,
                        past_key_values_data,
                        current_length_data,
                    )
                
                accept_length_tree = input_ids.shape[1] - cur_length
                cur_length = accept_length_tree + cur_length
                accept_lengths_tree.append(accept_length_tree)
                if model.tokenizer.eos_token_id in input_ids[0, input_len:] or new_token > max_new_token:
                    break

            torch.cuda.synchronize()
            total_time = time.time() - start_time
            avg_time_per_token = total_time / (new_token.cpu())
            avg_time_per_token_list.append(avg_time_per_token)
            avg_time_per_token_list_micro.append((total_time, new_token.cpu()))
            
            accept_lengths_tree_average.append(np.mean(accept_lengths_tree))
            accept_lengths_tree_average_micro.extend(accept_lengths_tree)

    print("accept_lengths_tree_average: ", np.mean(accept_lengths_tree_average))
    print("accept_lengths_tree_average_micro: ", np.mean(accept_lengths_tree_average_micro))
    print("avg_time_per_token: ", np.mean(avg_time_per_token_list))
    print("avg_time_per_token_micro: ", np.sum([item[0] for item in avg_time_per_token_list_micro]) / np.sum([item[1] for item in avg_time_per_token_list_micro]))
    print("*"*30)
    print()

    with open(accept_length_fpath, "w") as f:
        f.write(f"{np.mean(accept_lengths_tree_average)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--model-path",
        type=str,
        default="codellama/CodeLlama-7b-instruct-hf",
        help="The path to the weights. This can be a local folder or a Hugging Face repo ID.",
    )
    parser.add_argument(
        "--dataset-path",
        type=str,
        default="./human_eval/HumanEval.jsonl.gz",
        help="The path to the HumanEval dataset",
    )
    parser.add_argument(
        "--max-new-token",
        type=int,
        default=512,
        help="The maximum number of new generated tokens.",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.0,
        help="The temperature for sampling.",
    )

    parser.add_argument(
        "--top-p",
        type=float,
        default=0.0,
        help="The threshold for nucleus sampling.",
    )

    # REST's hyperparameters
    parser.add_argument(
        "--datastore-path",
        type=str,
        required=True,
        help="The path of the datastore for retrival.",
    )

    parser.add_argument(
        "--num-draft",
        type=int,
        default=64,
        help="The number of draft tokens.",
    )
    parser.add_argument(
        "--max-token-span",
        type=int,
        default=16,
        help="The maximum length of suffix for retrieval.",
    )
    parser.add_argument(
        "-n",
        "--ngram-n",
        type=int,
    )
    parser.add_argument(
        "-a",
        "--include-all",
        action="store_true",
    )
    parser.add_argument(
        "-t",
        "--num-top-ngrams",
        type=int,
    )
    parser.add_argument(
        "-m",
        "--merge-ratio",
        type=float,
    )
    parser.add_argument(
        "-o",
        "--accept-length-fpath",
        type=str,
    )
    parser.add_argument(
        "-b",
        "--num-benchmark-convs",
        type=int,
    )

    args = parser.parse_args()

    if args.temperature == 0:
        args.top_p = 0
        
    print(args)

    model = RestModel.from_pretrained(
        args.model_path,
        torch_dtype=torch.float16,
        low_cpu_mem_usage=True,
        device_map="auto"
    )

    tokenizer = model.get_tokenizer()

    dataset = HumanEvalDataset(args.dataset_path)

    print("loading the datastore ...")
    datastore = draftretriever.Reader(
                index_file_path=args.datastore_path,
            )
    print("datastore loaded!")

    ngram_datastore_settings = NGramDatastoreSettings("bigcode/the-stack-dedup", args.ngram_n, args.include_all, 0, args.num_top_ngrams, args.merge_ratio)
    
    run_eval(
        model, 
        tokenizer, 
        datastore, 
        args.max_token_span,
        args.num_draft,
        args.temperature, 
        args.top_p,
        args.max_new_token,
        ngram_datastore_settings,
        args.accept_length_fpath,
        args.num_benchmark_convs,
    )
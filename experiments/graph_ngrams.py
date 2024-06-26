import sys
import pandas as pd
import matplotlib.pyplot as plt

# Reading the CSV file
csv_fpath = sys.argv[1]
dataset = sys.argv[2]
data = pd.read_csv(csv_fpath)

# Plotting
plt.figure(figsize=(10, 6))
plt.plot(data['i'], data['three'], label='3-grams', color='blue', marker='o')
plt.plot(data['i'], data['four'], label='4-grams', color='red', marker='x')

# Adding titles and labels
plt.title('# of Unique N-Grams')
plt.xlabel('# of Conversations')
plt.ylabel('# Unique')
plt.legend()

# Show plot
plt.grid(True)
if str(dataset) == "large":
    plt.savefig('experiments/ultra_chat_graph.png')
else:
    plt.savefig('experiments/sharegpt_chat_graph.png')

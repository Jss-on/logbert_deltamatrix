import numpy as np
import scipy.stats as stats
import seaborn as sns
import matplotlib.pyplot as plt
import pickle
import time
import torch
from tqdm import tqdm
from torch.utils.data import DataLoader

from bert_pytorch.dataset import WordVocab
from bert_pytorch.dataset import LogDataset
from bert_pytorch.dataset.sample import fixed_window
import ast
from logparser import Spell, Drain

data_dir = 'input'
output_dir = "../output/bgl/"
sample_file = "sample.log"


def compute_anomaly(results, params, seq_threshold=0.5):
    is_logkey = params["is_logkey"]
    is_time = params["is_time"]
    total_errors = 0
    for seq_res in results:
        # label pairs as anomaly when over half of masked tokens are undetected
        if (is_logkey and seq_res["undetected_tokens"] > seq_res["masked_tokens"] * seq_threshold) or \
                (is_time and seq_res["num_error"]> seq_res["masked_tokens"] * seq_threshold) or \
                (params["hypersphere_loss_test"] and seq_res["deepSVDD_label"]):
            total_errors += 1
    return total_errors




class Inference():
    def __init__(self, options):
        self.model_path = options["model_path"]
        self.vocab_path = options["vocab_path"]
        self.device = options["device"]
        self.window_size = options["window_size"]
        self.adaptive_window = options["adaptive_window"]
        self.seq_len = options["seq_len"]
        self.corpus_lines = options["corpus_lines"]
        self.on_memory = options["on_memory"]
        self.batch_size = options["batch_size"]
        self.num_workers = options["num_workers"]
        self.num_candidates = options["num_candidates"]
        self.output_dir = options["output_dir"]
        self.model_dir = options["model_dir"]
        self.gaussian_mean = options["gaussian_mean"]
        self.gaussian_std = options["gaussian_std"]

        self.is_logkey = options["is_logkey"]
        self.is_time = options["is_time"]
        self.scale_path = options["scale_path"]

        self.hypersphere_loss = options["hypersphere_loss"]
        self.hypersphere_loss_test = options["hypersphere_loss_test"]

        self.lower_bound = self.gaussian_mean - 3 * self.gaussian_std
        self.upper_bound = self.gaussian_mean + 3 * self.gaussian_std

        self.center = None
        self.radius = None
        self.test_ratio = options["test_ratio"]
        self.mask_ratio = options["mask_ratio"]
        self.min_len=options["min_len"]

    def detect_logkey_anomaly(self, masked_output, masked_label):
        num_undetected_tokens = 0
        output_maskes = []
        for i, token in enumerate(masked_label):
            # output_maskes.append(torch.argsort(-masked_output[i])[:30].cpu().numpy()) # extract top 30 candidates for mask labels

            if token not in torch.argsort(-masked_output[i])[:self.num_candidates]:
                num_undetected_tokens += 1

        return num_undetected_tokens, [output_maskes, masked_label.cpu().numpy()]

    @staticmethod
    def generate_test(output_dir, file_name, window_size, adaptive_window, seq_len, scale, min_len):
        """
        :return: log_seqs: num_samples x session(seq)_length, tim_seqs: num_samples x session_length
        """
        log_seqs = []
        tim_seqs = []
        with open(output_dir + file_name, "r") as f:
            for idx, line in tqdm(enumerate(f.readlines())):
                #if idx > 40: break
                log_seq, tim_seq = fixed_window(line, window_size,
                                                adaptive_window=adaptive_window,
                                                seq_len=seq_len, min_len=min_len)
                if len(log_seq) == 0:
                    continue

                # if scale is not None:
                #     times = tim_seq
                #     for i, tn in enumerate(times):
                #         tn = np.array(tn).reshape(-1, 1)
                #         times[i] = scale.transform(tn).reshape(-1).tolist()
                #     tim_seq = times

                log_seqs += log_seq
                tim_seqs += tim_seq

        # sort seq_pairs by seq len
        log_seqs = np.array(log_seqs)
        tim_seqs = np.array(tim_seqs)

        test_len = list(map(len, log_seqs))
        test_sort_index = np.argsort(-1 * np.array(test_len))

        log_seqs = log_seqs[test_sort_index]
        tim_seqs = tim_seqs[test_sort_index]

        print(f"{file_name} size: {len(log_seqs)}")
        return log_seqs, tim_seqs

    def helper(self, model, output_dir, file_name, vocab, scale=None, error_dict=None):
        total_results = []
        total_errors = []
        output_results = []
        total_dist = []
        output_cls = []
        logkey_test, time_test = self.generate_test(output_dir, file_name, self.window_size, self.adaptive_window, self.seq_len, scale, self.min_len)

        # use 1/10 test data
        if self.test_ratio != 1:
            num_test = len(logkey_test)
            rand_index = torch.randperm(num_test)
            rand_index = rand_index[:int(num_test * self.test_ratio)] if isinstance(self.test_ratio, float) else rand_index[:self.test_ratio]
            logkey_test, time_test = logkey_test[rand_index], time_test[rand_index]


        seq_dataset = LogDataset(logkey_test, time_test, vocab, seq_len=self.seq_len,
                                 corpus_lines=self.corpus_lines, on_memory=self.on_memory, predict_mode=True, mask_ratio=self.mask_ratio)

        # use large batch size in test data
        data_loader = DataLoader(seq_dataset, batch_size=self.batch_size, num_workers=self.num_workers,
                                 collate_fn=seq_dataset.collate_fn)

        for idx, data in enumerate(data_loader):
            data = {key: value.to(self.device) for key, value in data.items()}

            result = model(data["bert_input"], data["time_input"])

            # mask_lm_output, mask_tm_output: batch_size x session_size x vocab_size
            # cls_output: batch_size x hidden_size
            # bert_label, time_label: batch_size x session_size
            # in session, some logkeys are masked

            mask_lm_output, mask_tm_output = result["logkey_output"], result["time_output"]
            output_cls += result["cls_output"].tolist()

            # dist = torch.sum((result["cls_output"] - self.hyper_center) ** 2, dim=1)
            # when visualization no mask
            # continue

            # loop though each session in batch
            for i in range(len(data["bert_label"])):
                seq_results = {"num_error": 0,
                               "undetected_tokens": 0,
                               "masked_tokens": 0,
                               "total_logkey": torch.sum(data["bert_input"][i] > 0).item(),
                               "deepSVDD_label": 0
                               }

                mask_index = data["bert_label"][i] > 0
                num_masked = torch.sum(mask_index).tolist()
                seq_results["masked_tokens"] = num_masked

                if self.is_logkey:
                    num_undetected, output_seq = self.detect_logkey_anomaly(
                        mask_lm_output[i][mask_index], data["bert_label"][i][mask_index])
                    seq_results["undetected_tokens"] = num_undetected

                    output_results.append(output_seq)

                if self.hypersphere_loss_test:
                    # detect by deepSVDD distance
                    assert result["cls_output"][i].size() == self.center.size()
                    # dist = torch.sum((result["cls_fnn_output"][i] - self.center) ** 2)
                    dist = torch.sqrt(torch.sum((result["cls_output"][i] - self.center) ** 2))
                    total_dist.append(dist.item())

                    # user defined threshold for deepSVDD_label
                    seq_results["deepSVDD_label"] = int(dist.item() > self.radius)
                    #
                    # if dist > 0.25:
                    #     pass

                if idx < 10 or idx % 1000 == 0:
                    print(
                        "{}, #time anomaly: {} # of undetected_tokens: {}, # of masked_tokens: {} , "
                        "# of total logkey {}, deepSVDD_label: {} \n".format(
                            file_name,
                            seq_results["num_error"],
                            seq_results["undetected_tokens"],
                            seq_results["masked_tokens"],
                            seq_results["total_logkey"],
                            seq_results['deepSVDD_label']
                        )
                    )
                total_results.append(seq_results)

        # for time
        # return total_results, total_errors

        #for logkey
        # return total_results, output_results

        # for hypersphere distance
        return total_results, output_cls

    def parse_sample(self, input_dir, output_dir, sample_file, parser_type = "drain"):
      log_format = '<Content>'#<Code1> <Time> <Code2> <Component1> <Component2> <Level> <Content>'
      regex = [
          r'(0x)[0-9a-fA-F]+', #hexadecimal
          r'\d+.\d+.\d+.\d+',
          # r'/\w+( )$'
          r'\d+'
      ]
      keep_para = False
      if parser_type == "drain":
          # the hyper parameter is set according to http://jmzhu.logpai.com/pub/pjhe_icws2017.pdf
          st = 0.3  # Similarity threshold
          depth = 3  # Depth of all leaf nodes
          parser = Drain.LogParser(log_format, indir=input_dir, outdir=output_dir, depth=depth, st=st, rex=regex, keep_para=keep_para)
          list_ids = parser.parse_sample(sample_file)
      elif parser_type == "spell":
          tau = 0.55
          parser = Spell.LogParser(indir=data_dir, outdir=output_dir, log_format=log_format, tau=tau, rex=regex, keep_para=keep_para)
          parser.parse(sample_file)

      return list_ids

    def predict_single_sequence(self):#, logkey_sequence_file):
      # logkey_sequence = ast.literal_eval(logkey_sequence_str)
      # Load the model and vocabulary
      # Read the log keys from the file
      
      # with open(logkey_sequence_file, 'r') as f:
      #     logkey_sequence_str = f.read().strip()
      # logkey_sequence = logkey_sequence_str.split()
      #       data_dir = 'input'
      # output_dir = "../output/bgl/"
      # sample_file = "sample.log"
      logkey_sequence = self.parse_sample(data_dir, output_dir, sample_file)
      print(logkey_sequence)
      model = torch.load(self.model_path)
      model.to(self.device)
      model.eval()
      vocab = WordVocab.load_vocab(self.vocab_path)

      # Convert your single sequence of log keys into the format expected by the model
      logkey_test = [logkey_sequence]  # Wrap your sequence in a list
      time_test = [[0]*len(logkey_sequence)]  # Create a dummy list of timestamps

      # print(logkey_sequence)  # Should output: 500

      # Create a DataLoader for your single sequence
      seq_dataset = LogDataset(logkey_test, time_test, vocab, seq_len=len(logkey_sequence),
                              corpus_lines=self.corpus_lines, on_memory=self.on_memory, predict_mode=True, mask_ratio=self.mask_ratio)
      print(len(seq_dataset[0][0]))  # Should output: 500

      data_loader = DataLoader(seq_dataset, batch_size=1, num_workers=1, collate_fn=seq_dataset.collate_fn)
      print(data_loader)
      # Iterate over the DataLoader (there will only be one batch since it's a single sequence)
      for data in data_loader:
          data = {key: value.to(self.device) for key, value in data.items()}
          print(data['bert_input'].shape)
          # Apply the model to the data and collect the results
          result = model(data["bert_input"], data["time_input"])
          mask_lm_output = result["logkey_output"]

          
          # Check for anomalies in the log keys
          mask_index = data["bert_label"][0] > 0
          num_undetected, output_seq = self.detect_logkey_anomaly(mask_lm_output[0][mask_index], data["bert_label"][0][mask_index])
          num_masked = torch.sum(mask_index).tolist()
          # If there are any undetected tokens, the sequence is abnormal
          if num_undetected > 10:
              print("The sequence is abnormal.")
          else:
              print("The sequence is normal.")

          print(f'Undetected Tokens: {num_undetected}')
          print(f"Masked Tokens: {num_masked}")
          print(f"Total Logkey: {torch.sum(data['bert_input'][0] > 0).item()}")
          print(f"Output sequence Length: {len(output_seq)}")
          print(f"Output_sequence: {output_seq}")
         
    



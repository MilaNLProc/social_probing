import numpy as np
import pandas as pd
from collections import defaultdict
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import LabelEncoder
import torch
from tqdm import tqdm
from pytorchtools import EarlyStopping
from torch import nn
from typing import List
from os.path import exists
from transformers import *
from sklearn.metrics import f1_score
from torch.utils.data import DataLoader
from custom_dataset import ProbingDataset
from datasets import Dataset
from custom_dataset import *
import pickle

class MLP(nn.Module):
    """
    Basic MLP, should be the same used in other probing papers
    """

    def __init__(self, input_size, output_size, hiddens):
        super().__init__()
        self.layers = nn.Sequential(
            nn.Linear(input_size, hiddens),
            nn.ReLU(),
            nn.Linear(hiddens, hiddens),
            nn.ReLU(),
            nn.Linear(hiddens, output_size)
        )

    def forward(self, x):
        return self.layers(x)

class ClassicalProber:
    """
    Prober based on the classical framework
    """
    def __init__(self, embedding_size):
        self.embedding_size = embedding_size

    def run(self, path, ):

        with open(path, "rb") as filino:
            data = pickle.load(filino)

        layers = list(data.keys())
        layers.remove("labels")

        le = LabelEncoder()
        labels = le.fit_transform(data["labels"])
        results = {}
        for l in layers:
            train_X, evaluation_X, train_y, evaluation_y = train_test_split(data[l], labels,
                                                                            test_size = 0.2,
                                                                            random_state = 42)
            eval_X, test_X, eval_y, test_y = train_test_split(evaluation_X, evaluation_y,
                                                              test_size = 0.5, random_state = 42)

            results[l] = self.train_and_test(train_X, train_y, test_X, test_y, eval_X, eval_y, output_size=len(labels),
                                             hiddens=50)

        return results


    def train_and_test(self, train_X, train_y, test_X, test_y, eval_X, eval_Y, output_size, hiddens=100, epochs=200,
                       patience=1):
        early_stopping = EarlyStopping(patience=patience, verbose=True)
        valid_loss = 20000000

        train_dataset = ProbingDataset(train_X, train_y)
        trainloader = torch.utils.data.DataLoader(train_dataset, batch_size=4)

        valid_dataset = ProbingDataset(eval_X, eval_Y)
        validloader = torch.utils.data.DataLoader(valid_dataset, batch_size=4)

        test_dataset = ProbingDataset(test_X, test_y)
        testloader = torch.utils.data.DataLoader(test_dataset, batch_size=4)

        mlp = MLP(self.embedding_size, output_size, hiddens)

        loss_function = nn.CrossEntropyLoss()
        optimizer = torch.optim.Adam(mlp.parameters(), lr=5e-5)

        for epoch in range(0, epochs):
            for i, data in enumerate(trainloader, 0):
                mlp.train()
                inputs, targets = data

                optimizer.zero_grad()
                outputs = mlp(inputs)

                loss = loss_function(outputs, targets)
                loss.backward()

                optimizer.step()

                if i % 50 == 0:
                    valid_loss = 0
                    mlp.eval()
                    with torch.no_grad():

                        for i, data in enumerate(validloader, 0):
                            inputs, targets = data

                            optimizer.zero_grad()
                            outputs = mlp(inputs)

                            valid_loss += loss_function(outputs, targets)

            early_stopping(valid_loss, mlp)

            if early_stopping.early_stop:
                print("Early stopping")
                break

        predictions = []

        with torch.no_grad():
            labels = []
            for i, data in enumerate(testloader, 0):
                inputs, targets = data
                outputs = mlp(inputs)

                predictions.extend(np.argmax(outputs.detach().numpy(), axis=1).tolist())
                labels.extend(targets.numpy().tolist())
        return f1_score(labels, predictions, average="macro")


class MLDProber:
    """
    Prober based on MLD
    """
    def __init__(self, embedder, embedding_size):
        """
        :param embedder: SentenceTransformer embedding model
        :param embedding_size: embedding size of the embedding generated by the sentence transformer models
        """
        self.embedder = embedder
        self.embedding_size = embedding_size


    def run(self, text, labels):
        le = LabelEncoder()

        labels = le.fit_transform(labels)
        data = pd.DataFrame({"text": text, "labels": labels}) # should we seed-shuffle here?
        number_of_labels = len(set(labels))

        portions = [0, 0.1, 0.2, 0.4, 0.8, 1.6, 3.2, 6.25, 12.5, 25, 100]
        number_of_examples = len(data)

        code_length_first_portion = int(portions[1] * number_of_examples / 100) * np.log2(number_of_labels)

        sum_of_losses = 0
        for index, p in enumerate(portions):

            # we train on portion (i, i +1) and we test on

            if p >= 25:
                # from this point there is no other portion to train on
                continue

            train_start_index = int(portions[index] * number_of_examples / 100)
            train_end_index = int(portions[index + 1] * number_of_examples / 100)

            test_start_index = int(portions[index + 1] * number_of_examples / 100)

            print(f"training on partition from {portions[index]} to {portions[index + 1]}")
            print(f"testing on partition {portions[index + 1]} to the next one")

            # just checking not to go beyond the 100%
            if index > len(portions) - 2:
                test_end_index = -1
            else:
                test_end_index = int(portions[index + 2] * number_of_examples / 100)

            train_portion = data.iloc[train_start_index:train_end_index]
            test_portion = data.iloc[test_start_index:test_end_index]

            sum_of_losses += self.get_loss(train_portion["text"].values.tolist(),
                                           test_portion["labels"].values.tolist(),
                                           test_portion["text"].values.tolist(),
                                           test_portion["labels"].values.tolist(), number_of_labels)

        return {"code_length": code_length_first_portion, "sum_of_losses": sum_of_losses}

    def get_loss(self, train_X, train_y, test_X, test_y, output_size, hiddens=50, epochs=200):
        """
        Simply training
        :param train_X:
        :param train_y:
        :param test_X:
        :param test_y:
        :param output_size:
        :param hiddens:
        :param epochs:
        :return:
        """
        embedding_train = self.embedder.encode(train_X)
        embedding_test = self.embedder.encode(test_X)

        train_dataset = ProbingDataset(embedding_train, train_y)
        trainloader = torch.utils.data.DataLoader(train_dataset, batch_size=4)

        test_dataset = ProbingDataset(embedding_test, test_y)
        testloader = torch.utils.data.DataLoader(test_dataset, batch_size=4)

        mlp = MLP(self.embedding_size, output_size, hiddens)

        loss_function = nn.CrossEntropyLoss()
        optimizer = torch.optim.Adam(mlp.parameters(), lr=5e-5)

        for epoch in range(0, epochs):
            for i, data in enumerate(trainloader, 0):
                inputs, targets = data

                optimizer.zero_grad()
                outputs = mlp(inputs)

                loss = loss_function(outputs, targets)
                loss.backward()

                optimizer.step()

        final_loss = 0.0

        with torch.no_grad():
            for i, data in enumerate(testloader, 0):
                inputs, targets = data
                optimizer.zero_grad()
                outputs = mlp(inputs)

                loss = loss_function(outputs, targets)

                final_loss += loss.item()

        return final_loss


class Embedder:

    def __init__(self, embedding_model):
        self.tokenizer = AutoTokenizer.from_pretrained(embedding_model)
        self.model = AutoModel.from_pretrained(embedding_model, output_hidden_states=True).to("cuda")



    def mean_pooling(self, token_embeddings, attention_mask):
        input_mask_expanded = attention_mask.unsqueeze(-1).expand(token_embeddings.size()).float()
        return torch.sum(token_embeddings * input_mask_expanded, 1) / torch.clamp(input_mask_expanded.sum(1), min=1e-9)

    def create_embeddings(self, texts, labels, layers: List, embedding_path):

        file_exists = exists(embedding_path)

        if file_exists:
            raise Exception("File Already Exists")

        df = pd.DataFrame({"texts": texts})

        train_dataset = Dataset.from_pandas(df)
        train_dataset = prepare_dataset(train_dataset, self.tokenizer)

        saving_dict = defaultdict(list)
        train_loader = DataLoader(train_dataset, batch_size=4)

        pbar = tqdm(total=len(train_loader), position=0)
        with torch.no_grad():
            self.model.eval()
            for batch in train_loader:
                pbar.update(1)
                batch = {k: v.to("cuda") for k, v in batch.items()}
                preds = self.model(**batch)

                for layer in layers:
                    mean_pooling = self.mean_pooling(preds["hidden_states"][layer], batch["attention_mask"]).detach()
                    mean_pooling = mean_pooling.cpu()
                    mean_pooling = mean_pooling.numpy()
                    saving_dict[layer].extend(mean_pooling)

        pbar.close()

        saving_dict["labels"] = labels
        saving_dict = dict(saving_dict)
        with open(f"{embedding_path}", "wb") as filino:
            pickle.dump(saving_dict, filino)


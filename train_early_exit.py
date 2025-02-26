import gzip
import random
from functools import partial, wraps

import numpy as np
import torch
import tqdm
from torch.cuda import Event
from torch.optim import Adam
from torch.utils.data import DataLoader, Dataset

timer = partial(Event, enable_timing=True)

from speculative_decoding import (
    Decoder,
    base_decoding,
    speculative_decoding_with_same_model,
)

# constants

NUM_BATCHES = int(1e5)
BATCH_SIZE = 4
GRAD_ACCUM_EVERY = 4
LEARNING_RATE = 1e-4
VALIDATE_EVERY = 100
PRIME_LENGTH = 128
GENERATE_EVERY = 500
GENERATE_LENGTH = 512
SEQ_LEN = 512
GAMMA = 5
EARLY_EXIT_LOSS_WEIGHT = 1.0

DEVICE_STR = "cuda" if torch.cuda.is_available() else "cpu"

# helpers


def cycle(loader):
    while True:
        for data in loader:
            yield data


def decode_token(token):
    return str(chr(max(32, token)))


def decode_tokens(tokens):
    return "".join(list(map(decode_token, tokens)))


def benchmark(fn):
    @wraps(fn)
    def inner(*args, **kwargs):
        start_event = timer()
        end_event = timer()
        start_event.record()

        out = fn(*args, **kwargs)

        end_event.record()
        torch.cuda.synchronize()
        elapsed_time_ms = start_event.elapsed_time(end_event)
        return out, elapsed_time_ms

    return inner


# instantiate transformer

device = torch.device(DEVICE_STR)

model = Decoder(
    num_tokens=256,
    dim=512,
    depth=10,
    early_exit_layer=2,  # use the same model as the small approximate model, worry about caching layer hiddens later
).to(device)

# prepare enwik8 data

with gzip.open("./data/enwik8.gz") as file:
    data = np.frombuffer(file.read(int(95e6)), dtype=np.uint8).copy()
    np_train, np_valid = np.split(data, [int(90e6)])
    data_train, data_val = torch.from_numpy(np_train), torch.from_numpy(np_valid)


class TextSamplerDataset(Dataset):
    def __init__(self, data, seq_len):
        super().__init__()
        self.data = data
        self.seq_len = seq_len

    def __getitem__(self, index):
        rand_start = torch.randint(0, self.data.size(0) - self.seq_len, (1,))
        full_seq = self.data[rand_start : rand_start + self.seq_len + 1].long()
        return full_seq.to(device)

    def __len__(self):
        return self.data.size(0) // self.seq_len


train_dataset = TextSamplerDataset(data_train, SEQ_LEN)
val_dataset = TextSamplerDataset(data_val, SEQ_LEN)
train_loader = cycle(DataLoader(train_dataset, batch_size=BATCH_SIZE))
val_loader = cycle(DataLoader(val_dataset, batch_size=BATCH_SIZE))

# optimizer

optim = Adam(model.parameters(), lr=LEARNING_RATE)

# training

for i in tqdm.tqdm(range(NUM_BATCHES), mininterval=10.0, desc="training"):
    model.train()

    for _ in range(GRAD_ACCUM_EVERY):
        data = next(train_loader)

        loss, small_loss = model(data, return_loss=True)

        ((loss + small_loss * EARLY_EXIT_LOSS_WEIGHT) / GRAD_ACCUM_EVERY).backward()

    print(f"training loss: {loss.item():.3f}")
    print(f"training small loss: {small_loss.item():.3f}")

    torch.nn.utils.clip_grad_norm_(model.parameters(), 0.5)

    optim.step()
    optim.zero_grad()

    if i % VALIDATE_EVERY == 0:
        model.eval()
        with torch.no_grad():
            valid_data = next(val_loader)

            loss, small_loss = model(valid_data, return_loss=True)
            print(f"validation loss: {loss.item():.3f}")
            print(f"validation small loss: {small_loss.item():.3f}")

    if i % GENERATE_EVERY == 0:
        model.eval()

        inp = random.choice(val_dataset)[:PRIME_LENGTH]
        prime = decode_tokens(inp)
        print(f"%s \n\n %s", (prime, "*" * 100))

        prompt = inp[None, ...]

        sampled, base_decode_elapsed = benchmark(base_decoding)(
            model, prompt, GENERATE_LENGTH
        )

        (spec_decode_sampled, num_accepted), spec_decode_elapsed = benchmark(
            speculative_decoding_with_same_model
        )(model, prompt, GENERATE_LENGTH, GAMMA)

        base_decode_output = decode_tokens(sampled[0])
        spec_decode_output = decode_tokens(spec_decode_sampled[0])

        print("\nbase decoding:\n\n", base_decode_output, "\n")
        print("\nspec decoding:\n\n", spec_decode_output, "\n")

        print(f"base decoding in: {base_decode_elapsed:.3f}ms\n")
        print(f"spec decoding in: {spec_decode_elapsed:.3f}ms\n")
        print(f"average num accepted: {num_accepted:.1f} / {GAMMA}\n")

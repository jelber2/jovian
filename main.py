#!/usr/bin/env python


import logging
from datetime import datetime
from collections import defaultdict

import pysam
import torch
from torch import nn
import torch.multiprocessing as mp
import numpy as np
import argparse
import yaml

import sim
import util
import vcf
import loader
from bam import target_string_to_tensor, encode_pileup2, reads_spanning, alnstart
from model import VarTransformer


logging.basicConfig(format='[%(asctime)s]  %(name)s  %(levelname)s  %(message)s',
                    datefmt='%m-%d %H:%M:%S',
                    level=logging.INFO) # handlers=[RichHandler()])
logger = logging.getLogger(__name__)

DEVICE = torch.device("cuda:0") if hasattr(torch, 'cuda') and torch.cuda.is_available() else torch.device("cpu")




def trim_pileuptensor(src, tgt, width):
    """
    Trim or zero-pad the sequence dimension of src and target (first dimension of src, second of target)
    so they're equal to width
    :param src: Data tensor of shape [sequence, read, features]
    :param tgt: Target tensor of shape [haplotype, sequence]
    :param width: Size to trim to
    """
    assert src.shape[0] == tgt.shape[1], f"Unequal src and target lengths ({src.shape[0]} vs. {tgt.shape[1]}), not sure how to deal with this :("
    if src.shape[0] < width:
        z = torch.zeros(width - src.shape[0], src.shape[1], src.shape[2])
        src = torch.cat((src, z))
        t = torch.zeros(tgt.shape[0], width - tgt.shape[1])
        tgt = torch.cat((tgt, t), dim=1)
    else:
        start = src.shape[0] // 2 - width // 2
        src = src[start:start+width, :, :]
        tgt = tgt[:, start:start+width]

    return src, tgt


def make_loader(bampath, refpath, csv, max_to_load=1e9, max_reads_per_aln=100):
    allsrc = []
    alltgt = []
    count = 0
    seq_len = 150
    logger.info(f"Creating new data loader from {bampath}")
    counter = defaultdict(int)
    classes = []
    for enc, tgt, status, vtype in loader.load_from_csv(bampath, refpath, csv, max_reads_per_aln=max_reads_per_aln):
        label_class = "-".join((status, vtype))
        classes.append(label_class)
        counter[label_class] += 1
        src, tgt = trim_pileuptensor(enc, tgt, seq_len)
        assert src.shape[0] == seq_len, f"Src tensor #{count} had incorrect shape after trimming, found {src.shape[0]} but should be {seq_len}"
        assert tgt.shape[1] == seq_len, f"Tgt tensor #{count} had incorrect shape after trimming, found {tgt.shape[1]} but should be {seq_len}"
        allsrc.append(src)
        alltgt.append(tgt)
        count += 1
        if count % 100 == 0:
            logger.info(f"Loaded {count} tensors from {csv}")
        if count == max_to_load:
            logger.info(f"Stopping tensor load after {max_to_load}")
            break
    logger.info(f"Loaded {count} tensors from {csv}")
    logger.info("Class breakdown is: " + " ".join(f"{k}={v}" for k,v in counter.items()))
    weights = np.array([1.0 / counter[c] for c in classes])
    return loader.WeightedLoader(torch.stack(allsrc), torch.stack(alltgt).long(), weights, DEVICE)


def make_multiloader(inputs, refpath, threads, max_to_load, max_reads_per_aln):
    """
    Create multiple ReadLoaders in parallel for each element in Inputs
    :param inputs: List of (BAM path, labels csv) tuples
    :param threads: Number of threads to use
    :param max_reads_per_aln: Max number of reads for each pileup
    :return: List of loaders
    """
    results = []
    logger.info(f"Loading training data for {len(inputs)} samples with {threads} processes (max to load = {max_to_load})")
    with mp.Pool(processes=threads) as pool:
        for bam, labels_csv in inputs:
            result = pool.apply_async(make_loader, (bam, refpath, labels_csv, max_to_load, max_reads_per_aln))
            results.append(result)
        pool.close()
        return loader.MultiLoader([l.get(timeout=2*60*60) for l in results])



def sort_by_ref(seq, reads):
    """
    Sort read tensors by how closely they match the sequence seq.
    :param seq:
    :param reads: Tensor of encoded reads, with dimension [batch, pos, read, feature]
    :return: New tensor containing the same read tensors, but sorted
    """
    results = []
    for batch in range(reads.shape[0]):
        w = reads[batch, :, :, 0:4].sum(dim=-1)
        t = reads[batch, :, :, 0:4].argmax(dim=-1)
        matchsum = (t == (seq[batch, 0, :].repeat(reads.shape[2], 1).transpose(0,1)*w).long()).sum(dim=0)
        results.append(reads[batch, :, torch.argsort(matchsum), :])
    return torch.stack(results)


def train_epoch(model, optimizer, criterion, loader, batch_size):
    """
    Train for one epoch, which is defined by the loader but usually involves one pass over all input samples
    :param model: Model to train
    :param optimizer: Optimizer to update params
    :param criterion: Loss function
    :param loader: Provides training data
    :param batch_size:
    :return: Sum of losses over each batch, plus fraction of matching bases for ref and alt seq
    """
    epoch_loss_sum = 0
    for unsorted_src, tgt in loader.iter_once(batch_size):
        src = sort_by_ref(tgt, unsorted_src)
        optimizer.zero_grad()

        tgt_seq1 = tgt[:, 0, :]
        tgt_seq2 = tgt[:, 1, :]

        seq1preds, seq2preds = model(src.flatten(start_dim=2))

        loss = criterion(seq1preds.flatten(start_dim=0, end_dim=1), tgt_seq1.flatten())
        loss += criterion(seq2preds.flatten(start_dim=0, end_dim=1), tgt_seq2.flatten())

        with torch.no_grad():
            width = 20
            mid = seq1preds.shape[1] // 2
            midmatch1 = (torch.argmax(seq1preds[:, mid-width//2:mid+width//2, :].flatten(start_dim=0, end_dim=1),
                                     dim=1) == tgt_seq1[:, mid-width//2:mid+width//2].flatten()
                         ).float().mean()
            midmatch2 = (
                        torch.argmax(seq1preds[:, mid - width // 2:mid + width // 2, :].flatten(start_dim=0, end_dim=1),
                                     dim=1) == tgt_seq1[:, mid - width // 2:mid + width // 2].flatten()
                        ).float().mean()
            # matches1 = (torch.argmax(seq1preds.flatten(start_dim=0, end_dim=1),
            #                          dim=1) == tgt_seq1.flatten()).float().mean()
            # matches2 = (torch.argmax(seq2preds.flatten(start_dim=0, end_dim=1),
            #                          dim=1) == tgt_seq2.flatten()).float().mean()

        loss.backward(retain_graph=True)
        optimizer.step()
        epoch_loss_sum += loss.detach().item()

    return epoch_loss_sum, midmatch1.item(), midmatch2.item()


def train_epochs(epochs, dataloader, max_read_depth=250, feats_per_read=7, init_learning_rate=0.001, statedict=None, model_dest=None):
    in_dim = max_read_depth * feats_per_read
    model = VarTransformer(in_dim=in_dim, out_dim=4, nhead=5, d_hid=200, n_encoder_layers=2).to(DEVICE)
    if statedict is not None:
        logger.info(f"Initializing model with state dict {statedict}")
        model.load_state_dict(torch.load(statedict))
    model.train()
    batch_size = 64

    criterion = nn.CrossEntropyLoss()
    optimizer = torch.optim.Adam(model.parameters(), lr=init_learning_rate)
    scheduler = torch.optim.lr_scheduler.StepLR(optimizer, 1.0, gamma=0.99)
    try:
        for epoch in range(epochs):
            starttime = datetime.now()
            loss, refmatch, altmatch = train_epoch(model, optimizer, criterion, dataloader, batch_size=batch_size)
            elapsed = datetime.now() - starttime
            logger.info(f"Epoch {epoch} Secs: {elapsed.total_seconds():.2f} lr: {scheduler.get_last_lr()[0]:.4f} loss: {loss:.4f} Ref match: {refmatch:.4f}  altmatch: {altmatch:.4f} ")
            scheduler.step()

        logger.info(f"Training completed after {epoch} epochs")
    except KeyboardInterrupt:
        pass

    if model_dest is not None:
        logger.info(f"Saving model state dict to {model_dest}")
        torch.save(model.to('cpu').state_dict(), model_dest)


def load_train_conf(confyaml):
    logger.info(f"Loading configuration from {confyaml}")
    conf = yaml.safe_load(open(confyaml).read())
    assert 'reference' in conf, "Expected 'reference' entry in training configuration"
    assert 'data' in conf, "Expected 'data' entry in training configuration"
    return conf


def train(config, output_model, input_model, epochs, max_to_load, **kwargs):
    logger.info(f"Found torch device: {DEVICE}")
    conf = load_train_conf(config)
    train_sets = [(c['bam'], c['labels']) for c in conf['data']]
    #dataloader = make_multiloader(train_sets, conf['reference'], threads=6, max_to_load=max_to_load, max_reads_per_aln=200)
    dataloader = loader.SimLoader(seqlen=150, readsperbatch=200, readlength=100)
    train_epochs(epochs, dataloader, max_read_depth=200, feats_per_read=7, statedict=input_model, model_dest=output_model)


def call(statedict, bam, reference, chrom, pos, **kwargs):
    max_read_depth = 200
    feats_per_read = 7
    in_dim = max_read_depth * feats_per_read
    model = VarTransformer(in_dim=in_dim, out_dim=4, nhead=5, d_hid=200, n_encoder_layers=2).to(DEVICE)
    model.load_state_dict(torch.load(statedict))
    model.eval()

    bam = pysam.AlignmentFile(bam)
    reads = reads_spanning(bam, chrom, pos, max_reads=max_read_depth)
    if len(reads) < 5:
        raise ValueError(f"Hmm, couldn't find any reads spanning {chrom}:{pos}")

    reads_encoded = encode_pileup2(reads).to(DEVICE)
    minref = min(alnstart(r) for r in reads)
    refseq = pysam.FastaFile(reference).fetch(chrom, minref, minref + reads_encoded.shape[0])
    reft = target_string_to_tensor(refseq).unsqueeze(0).unsqueeze(0).to(DEVICE)
    src = sort_by_ref(reft, reads_encoded.unsqueeze(0))
    
    print(util.to_pileup(src[0, :, :, :]))
    seq1preds, seq2preds = model(src.flatten(start_dim=2))
    pred1str = util.readstr(seq1preds[0, :, :])
    pred2str = util.readstr(seq2preds[0, :, :])
    print("\n")
    print(refseq)
    print(pred1str)
    print(pred2str)
    print("".join('*' if a==b else 'x' for a,b in zip(refseq, pred2str)))
    print(refseq)
    midwith = 100
    for v in vcf.align_seqs(refseq[len(refseq)//2 - midwith//2:len(refseq)//2 + midwith//2], 
                            pred2str[len(refseq)//2 - midwith//2:len(refseq)//2 + midwith//2],
                            offset=minref + len(refseq)//2 - midwith//2 + 1):
        print(v)

def eval_sim(statedict, **kwargs):
    max_read_depth = 200
    feats_per_read = 7
    in_dim = max_read_depth * feats_per_read
    model = VarTransformer(in_dim=in_dim, out_dim=4, nhead=5, d_hid=200, n_encoder_layers=2).to(DEVICE)
    model.load_state_dict(torch.load(statedict))
    model.eval()

    # SNVs
    batch_size = 10
    snv_src, snv_tgt = sim.make_batch(batch_size, 200, 200, 150, sim.make_het_snv, 0.02, clip_prob=0.02, vafs=0.95 * np.ones(batch_size))
    src = sort_by_ref(snv_tgt, snv_src)
    seq1preds, seq2preds = model(src.flatten(start_dim=2))

    midwidth = 100  # Only examine the 'middle' of the sequence with
    midstart = snv_tgt.shape[-1] // 2 - midwidth // 2
    midend = snv_tgt.shape[-1] // 2 + midwidth // 2

    for b in range(src.shape[0]):
        rseq1 = util.tgt_str(snv_tgt[b, 0, :])
        rseq2 = util.tgt_str(snv_tgt[b, 1, :])

        print(f"\n\n Batch {b}")

        actual_vars = []
        for v in vcf.align_seqs(rseq1[midstart:midend], rseq2[midstart:midend]):
            actual_vars.append(v)

        pred1_vars = []
        for v in vcf.align_seqs(rseq1[midstart:midend], util.readstr(seq1preds[b, midstart:midend])):
            pred1_vars.append(v)

        pred2_vars = []
        for v in vcf.align_seqs(rseq2[midstart:midend], util.readstr(seq2preds[b, midstart:midend])):
            pred2_vars.append(v)


        if pred1_vars:
            print("Found variants on pred 1:")
            for actual, predicted in zip(actual_vars, pred1_vars):
                print(f"Actual: {actual} predicts: {predicted}")
        else:
            print("No variants on pred 1")

        if pred2_vars:
            for actual, predicted in zip(actual_vars, pred2_vars):
                print(f"Actual: {actual} predicts: {predicted}")
        else:
            print(f"No variant detected on pred 2:(")



def main():
    parser = argparse.ArgumentParser()
    subparser = parser.add_subparsers()
    trainparser = subparser.add_parser("train", help="Train a model")
    trainparser.add_argument("-n", "--epochs", type=int, help="Number of epochs to train for", default=100)
    trainparser.add_argument("-i", "--input-model", help="Start with parameters from given state dict")
    trainparser.add_argument("-o", "--output-model", help="Save trained state dict here", required=True)
    trainparser.add_argument("-m", "--max-to-load", help="Max number of input tensors to load", type=int, default=1e9)
    trainparser.add_argument("-c", "--config", help="Training configuration yaml", required=True)
    trainparser.set_defaults(func=train)

    callparser = subparser.add_parser("call", help="Call variants")
    callparser.add_argument("-m", "--statedict", help="Stored model", required=True)
    callparser.add_argument("-r", "--reference", help="Path to Fasta reference genome", required=True)
    callparser.add_argument("-b", "--bam", help="Input BAM file", required=True)
    callparser.add_argument("--chrom", help="Chromosome", required=True)
    callparser.add_argument("--pos", help="Position", required=True, type=int)
    callparser.set_defaults(func=call)

    evalparser = subparser.add_parser("eval", help="Evaluate a model on some known or simulated data")
    evalparser.add_argument("-m", "--statedict", help="Stored model", required=True)
    evalparser.set_defaults(func=eval_sim)

    args = parser.parse_args()
    args.func(**vars(args))


if __name__ == "__main__":
    main()
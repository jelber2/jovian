#!/usr/bin/env python


import logging
import random
from collections import defaultdict, Counter
from dataclasses import dataclass
from pathlib import Path
from string import ascii_letters, digits
import gzip
import lz4.frame
import tempfile
from datetime import datetime
import re
import pandas as pd
import pyranges as pr

import pysam
import torch


import torch.multiprocessing as mp
from concurrent.futures.process import ProcessPoolExecutor
import numpy as np
import argparse
import yaml

import phaser
import util
import vcf
import loader
from bam import string_to_tensor, target_string_to_tensor, encode_pileup3, reads_spanning, alnstart, ensure_dim, \
    reads_spanning_range
from model import VarTransformer
from train import train, load_train_conf, eval_prediction


logging.basicConfig(format='[%(asctime)s]  %(name)s  %(levelname)s  %(message)s',
                    datefmt='%m-%d %H:%M:%S',
                    level=logging.INFO) # handlers=[RichHandler()])
logger = logging.getLogger(__name__)

DEVICE = torch.device("cuda") if hasattr(torch, 'cuda') and torch.cuda.is_available() else torch.device("cpu")


class LowReadCountException(Exception):
    """
    Region of bam file has too few spanning reads for variant detection
    """
    pass


def bed_to_windows(pr_bed, bed_slack=0, window_spacing=1000, window_overlap=0):
    """
    Make generator yielding windows of spacing window_spacing with right side overlap window_overlap
    Windows will typically be smaller than window_spacing at right end of bed intervals
    Also return total window count (might indicate how long it could take?)
    :param pr_bed: PyRange object representing bed file (columns Chromosome, Start, End)
    :param bed_slack: bases to slack both sides of each bed region
    :param window_spacing: spacing between the start of each window
    :param window_overlap: right side overlap between windows
    :return: yields Chromosome, Start, End of window
    """
    # merge and slack/pad bed file regions
    pr_slack = pr_bed.slack(bed_slack)
    df_windows = pr_slack.window(window_spacing).df
    df_windows["End"] = df_windows["End"] + window_overlap
    df_windows = pr.PyRanges(df_windows).intersect(pr_slack).df

    window_count = len(df_windows)
    windows = ((win.Chromosome, win.Start, win.End) for i, win in df_windows.iterrows())
    return windows, window_count


def reconcile_current_window(prev_win, current_win):
    """
    modify variant parameters in current window depending on any overlapping variants in previous window
    :param prev_win: variant dict for previous window (to left of current)
    :param current_win: variant dict for current window (most recent variants called)
    :return: modified variant dict for current window
    """
    overlap_vars = set(prev_win) & set(current_win)

    # swap haplotypes if supported by previous window
    same_hap_var_count, opposite_hap_var_count = 0, 0
    for v in overlap_vars:
        if prev_win[v].het and current_win[v].het:
            if prev_win[v].haplotype == current_win[v].haplotype:
                same_hap_var_count += 1
            else:
                opposite_hap_var_count += 1
    if opposite_hap_var_count > same_hap_var_count:  # swap haplotypes
        for k, v in current_win.items():
            current_win[k].genotype = tuple(reversed(current_win[k].genotype))
            if v.het and v.haplotype == 0:
                v.haplotype = 1
            elif v.het and v.haplotype == 1:
                v.haplotype = 0

    for var in overlap_vars:
        # if hom in both windows
        #   - just mark as DUPLICATE
        if not prev_win[var].het and not current_win[var].het:
            current_win[var].duplicate = True
        # if het in both windows and same genotype order ( 0|1 or 1|0 )
        #   - change phase set (PS) of current window to previous window
        #   - mark var as DUPLICATE in current window
        if prev_win[var].het and current_win[var].het and prev_win[var].genotype == current_win[var].genotype:
            current_win[var].duplicate = True
            for v in current_win:
                current_win[v].phase_set = prev_win[var].phase_set
        # if het in both windows and different haplotype (hap0 or hap1)
        #   - change phase set (PS) of current window to prev window
        #   - mark var as DUPLICATE in current window
        #   - reverse genotype of all current window vars (i.e., (0,1) to (1,0))
        if prev_win[var].het and current_win[var].het and prev_win[var].genotype != current_win[var].genotype:
            current_win[var].duplicate = True
    return current_win


def call(model_path, bam, bed, reference_fasta, vcf_out, bed_slack=0, window_spacing=1000, window_overlap=0, **kwargs):
    """
    Use model in statedict to call variants in bam in genomic regions in bed file.
    Steps:
      1. build model
      2. break bed regions into windows with start positions determined by window_spacing and end positions
         determined by window_overlap (the last window in each bed region will likely be shorter than others)
      3. call variants in each window
      4. join variants after searching for any duplicates
      5. save to vcf file
    :param statedict:
    :param bam:
    :param bed:
    :param reference_fasta:
    :param vcf_out:
    :param bed_slack:
    :param window_spacing:
    :param window_overlap:
    :param kwargs:
    :return:
      """


    max_read_depth = 100
    logger.info(f"Found torch device: {DEVICE}")
    logger.info(f"Loading model from path {model_path}")
    model = torch.jit.load(model_path)
    model.eval()


    reference = pysam.FastaFile(reference_fasta)
    aln = pysam.AlignmentFile(bam)
    pr_bed = pr.PyRanges(pd.read_csv(
        bed, sep="\t", names="Chromosome Start End".split(), usecols=[0, 1, 2], dtype=dict(chrom=str)
    )).merge()
    # make window generator from bed
    windows, windows_total_count = bed_to_windows(pr_bed, bed_slack=bed_slack, window_spacing=window_spacing, window_overlap=window_overlap)

    var_windows = []
    for i, (chrom, start, end) in enumerate(windows):
        vars_hap0, vars_hap1 = _call_vars_region(aln, model, reference, chrom, start, end, max_read_depth, window_size=300)

        # group vcf variants by window (list of dicts)
        # may get mostly empty dicts?
        var_windows.append(vcf.vcf_vars(vars_hap0=vars_hap0, vars_hap1=vars_hap1, chrom=chrom, window_idx=i, aln=aln, reference=reference))

        # compare most recent 2 windows to see if any overlapping variants
        # if so, modify phasing and remove duplicate calls
        if len(var_windows) > 1:  # start on second loop
            var_windows[-1] = reconcile_current_window(var_windows[-2], var_windows[-1])

        # add log update every so often
        log_spacing = 5000
        if (i + 1) % log_spacing == 0:
            logger.info(f"Called variants up to {chrom}:{start}, in {i + 1}  of {windows_total_count} total windows")

    # add one more log update at the end
    logger.info(f"Called variants up to {chrom}:{start}, in {i + 1}  of {windows_total_count} total windows")

    # convert to pyranges object for sorting, etc.
    vcfvar_list = []
    for var_window in var_windows:
        for var in var_window.values():
            vcfvar_list.append(var)
    df_vars = pd.DataFrame(vcfvar_list)
    df_vars["Chromosome"] = df_vars.chrom
    df_vars["End"] = df_vars.pos
    df_vars["Start"] = df_vars.pos - 1
    pr_vars = pr.PyRanges(df_vars).sort()

    # intersect vars with bed file
    pr_vars = pr_vars.intersect(pr_bed)

    # generate vcf out
    vcf_file = vcf.init_vcf(vcf_out, sample_name="sample", lowcov=30)
    vcf.vars_to_vcf(vcf_file, pr_vars)
    vcf_file.close()


def callvars(model, aln, reference, chrom, start, end, window_width, max_read_depth, min_reads=5):
    """
    Call variants in a region of a BAM file using the given altpredictor and model
    and return a list of vcf.Variant objects
    """
    reads = reads_spanning_range(aln, chrom, start, end)
    if len(reads) < min_reads:
        raise LowReadCountException(f"Hmm, couldn't find {min_reads} reads spanning {chrom}:{start}-{end}")
    if len(reads) > max_read_depth:
        reads = random.sample(reads, max_read_depth)
    reads = util.sortreads(reads)
    minref = min(alnstart(r) for r in reads)
    maxref = max(alnstart(r) + r.query_length for r in reads)
    reads_encoded, _ = encode_pileup3(reads, minref, maxref)

    refseq = reference.fetch(chrom, minref, minref + reads_encoded.shape[0])
    reftensor = string_to_tensor(refseq)
    reads_w_ref = torch.cat((reftensor.unsqueeze(1), reads_encoded), dim=1)
    padded_reads = ensure_dim(reads_w_ref, maxref - minref, max_read_depth).unsqueeze(0).to(DEVICE)
    midstart = max(0, start - minref)
    midend = midstart + window_width

    if padded_reads.shape[1] > window_width:
        padded_reads = padded_reads[:, midstart:midend, :, :]

    #masked_reads = padded_reads * fullmask
    seq_preds = model(padded_reads.float().to(DEVICE))
    return seq_preds[0, 0, :, :], seq_preds[0, 1, :, :], start


def _call_vars_region(aln, model, reference, chrom, start, end, max_read_depth, window_size=300, min_reads=5):
    """
    For the given region, identify variants by repeatedly calling the model over a sliding window,
    tallying all of the variants called, and passing back all call and repeat count info
    for further exploration
    Currently:
    - exclude all variants in the downstream half of the window
    - retain all remaining var calls noting how many time each one was called, qualities, etc.
    - call with no repeats are mostly false positives but they are retained
    - haplotype 0 and 1 for each step are set by comparing with repeat vars from previous steps

    TODO:
      - add prob info from alt sequence to vars?
      - add depth derived from tensor to vars?
      - create new prob from all duplicate calls?
    """
    window_step = 50
    var_retain_window_size = 150
    allvars0 = defaultdict(list)
    allvars1 = defaultdict(list)
    window_start = start - 2 * window_step # We start with regions a bit upstream of the focal / target region
    step_count = 0  # initialize
    while window_start <= (end - window_step):
        logger.info(f"Calling in {window_start} - {window_start + window_size}")
        # call vars
        try:
            hap0_t, hap1_t, offset = callvars(model, aln, reference, chrom, window_start, window_start + window_size, window_size,
                                              max_read_depth=max_read_depth, min_reads=min_reads)
            hap0 = util.readstr(hap0_t)
            hap1 = util.readstr(hap1_t)
            hap0_probs = hap0_t.detach().numpy().max(axis=-1)
            hap1_probs = hap1_t.detach().numpy().max(axis=-1)

            refseq = reference.fetch(chrom, offset, offset + window_size)
            vars_hap0 = list(vcf.aln_to_vars(refseq, hap0, offset, hap0_probs))
            vars_hap1 = list(vcf.aln_to_vars(refseq, hap1, offset, hap1_probs))
        except LowReadCountException:
            logger.debug(
                f"Bam window {chrom}:{window_start}-{window_start + window_size} "
                f"had too few reads for variant calling (< {min_reads})"
            )
            vars_hap0, vars_hap1 = [], []

        # put vars into hap0 and hap1 dicts
        stepvars0 = {}
        for v in vars_hap0:
            if v.pos < (window_start + var_retain_window_size):
                v.hap_model = 0
                v.step = step_count
                stepvars0[(v.pos, v.ref, v.alt)] = v
        stepvars1 = {}
        for v in vars_hap1:
            if v.pos < (window_start + var_retain_window_size):
                v.hap_model = 1
                v.step = step_count
                stepvars1[(v.pos, v.ref, v.alt)] = v

        # swap haplotypes if supported by previous vars
        same_hap_var_count = sum(len(allvars0[v]) for v in stepvars0 if v in allvars0)
        same_hap_var_count += sum(len(allvars1[v]) for v in stepvars1 if v in allvars1)
        opposite_hap_var_count = sum(len(allvars1[v]) for v in stepvars0 if v in allvars1)
        opposite_hap_var_count += sum(len(allvars0[v]) for v in stepvars1 if v in allvars0)
        if opposite_hap_var_count > same_hap_var_count:  # swap haplotypes
            stepvars1, stepvars0 = stepvars0, stepvars1

        # add this step's vars to allvars
        [allvars0[key].append(v) for key, v in stepvars0.items()]
        [allvars1[key].append(v) for key, v in stepvars1.items()]

        # continue
        window_start += window_step
        step_count += 1

    # Old method, only return vars that are called multiple times
    # hap0_passing = list(v[0] for k, v in allvars0.items() if len(v) > 1 and start < v[0].pos < end)
    # hap1_passing = list(v[0] for k, v in allvars1.items() if len(v) > 1 and start < v[0].pos < end)
    # return hap0_passing, hap1_passing

    # Return all vars even if they occur only once?
    return allvars0, allvars1


def load_conf(confyaml):
    logger.info(f"Loading configuration from {confyaml}")
    conf = yaml.safe_load(open(confyaml).read())
    assert 'reference' in conf, "Expected 'reference' entry in training configuration"
    assert 'data' in conf, "Expected 'data' entry in training configuration"
    return conf


def pregen_one_sample(dataloader, batch_size, output_dir):
    """
    Pregenerate tensors for a single sample
    """
    uid = "".join(random.choices(ascii_letters + digits, k=8))
    src_prefix = "src"
    tgt_prefix = "tgt"
    vaf_prefix = "vaftgt"
    metafile = tempfile.NamedTemporaryFile(
        mode="wt", delete=False, prefix="pregen_", dir=".", suffix=".txt"
    )
    logger.info(f"Saving tensors to {output_dir}/")
    for i, (src, tgt, vaftgt, varsinfo) in enumerate(dataloader.iter_once(batch_size)):
        logger.info(f"Saving batch {i} with uid {uid}")
        for data, prefix in zip([src, tgt, vaftgt],
                                [src_prefix, tgt_prefix, vaf_prefix]):
            with lz4.frame.open(output_dir / f"{prefix}_{uid}-{i}.pt.lz4", "wb") as fh:
                torch.save(data, fh)
        for idx, varinfo in enumerate(varsinfo):
            meta_str = "\t".join([
                f"{idx}", f"{uid}-{i}", "\t".join(varinfo), dataloader.csv
            ]) 
            print(meta_str, file=metafile)
        metafile.flush()

    metafile.close()
    return metafile.name


def default_vals_per_class():
    """
    Multiprocess will instantly deadlock if a lambda or any callable not defined on the top level of the module is given
    as the 'factory' argument to defaultdict - but we have to give it *some* callable that defines the behavior when the key
    is not present in the dictionary, so this returns the default "vals_per_class" if a class is encountered that is not 
    specified in the configuration file. I don't think there's an easy way to make this user-settable, unfortunately
    """
    return 0


def pregen(config, **kwargs):
    """
    Pre-generate tensors from BAM files + labels and save them in 'datadir' for quicker use in training
    (this takes a long time)
    """
    conf = load_conf(config)
    batch_size = kwargs.get('batch_size', 64)
    reads_per_pileup = kwargs.get('read_depth', 100)
    samples_per_pos = kwargs.get('samples_per_pos', 8)
    vals_per_class = defaultdict(default_vals_per_class)
    vals_per_class.update(conf['vals_per_class'])

    output_dir = Path(kwargs.get('dir'))
    metadata_file = kwargs.get("metadata_file", None)
    if metadata_file is None:
        str_time = datetime.now().strftime("%Y_%d_%m_%H_%M_%S")
        metadata_file = f"pregen_{str_time}.csv"
    processes = kwargs.get('threads', 1)

    logger.info(f"Generating training data using config from {config} vals_per_class: {vals_per_class}")
    dataloaders = [
            loader.LazyLoader(c['bam'], c['bed'], c['vcf'], conf['reference'], reads_per_pileup, samples_per_pos, vals_per_class)
        for c in conf['data']
    ]

    output_dir.mkdir(parents=True, exist_ok=True)
    logger.info(f"Submitting {len(dataloaders)} jobs with {processes} process(es)")

    meta_headers = ["item", "uid", "chrom", "pos", "ref", "alt", "vaf", "label"]
    with open(metadata_file, "wb") as metafh:
        metafh.write(("\t".join(meta_headers) + "\n").encode())
        if processes == 1:
            for dl in dataloaders:
                sample_metafile = pregen_one_sample(dl, batch_size, output_dir)
                util.concat_metafile(sample_metafile, metafh)
        else:
            futures = []
            with ProcessPoolExecutor(max_workers=processes) as executor:
                for dl in dataloaders:
                    futures.append(executor.submit(pregen_one_sample, dl, batch_size, output_dir))
            for fut in futures:
                sample_metafile = fut.result()
                util.concat_metafile(sample_metafile, metafh)



def eval_labeled_bam(config, bam, labels, statedict, truth_vcf, **kwargs):
    """
    Call variants in BAM file with given model at positions given in the labels CSV, emit useful
    summary information about PPA / PPV, etc
    """
    max_read_depth = 100
    feats_per_read = 9
    logger.info(f"Found torch device: {DEVICE}")
    conf = load_conf(config)

    reference = pysam.FastaFile(conf['reference'])
    truth_vcf = pysam.VariantFile(truth_vcf)
    attention_heads = 2
    transformer_dim = 400
    encoder_layers = 8
    embed_dim_factor = 200
    model = VarTransformer(read_depth=max_read_depth,
                                    feature_count=feats_per_read,
                                    out_dim=4,
                                    embed_dim_factor=embed_dim_factor,
                                    nhead=attention_heads,
                                    d_hid=transformer_dim,
                                    n_encoder_layers=encoder_layers,
                                    device=DEVICE)

    model.load_state_dict(torch.load(statedict, map_location=DEVICE))
    model.eval()

    aln = pysam.AlignmentFile(bam)
    results = defaultdict(Counter)
    window_size = 300

    tot_tps = 0
    tot_fps = 0
    tot_fns = 0
    results = defaultdict(Counter)
    for i, line in enumerate(open(labels)):
        tps = []
        fps = []
        fns = []
        toks = line.strip().split("\t")
        chrom = toks[0]
        start = int(toks[1])
        end = int(toks[2])
        label = toks[3]

        fp_varpos = []
        tp_varpos = []
        try:
            allvars0, allvars1 = _call_vars_region(aln, model, reference, chrom, start, end, max_read_depth=100,
                                                   window_size=300)
            # TODO   Keep only vars that occur more than once?
            vars_hap0 = list(v[0] for k, v in allvars0.items() if len(v) > 1 and start < v[0].pos < end)
            vars_hap1 = list(v[0] for k, v in allvars1.items() if len(v) > 1 and start < v[0].pos < end)

        except Exception as ex:
            logger.warning(f"Hmm, exception processing {chrom}:{start}-{end}, skipping it")
            logger.warning(ex)
            continue

        print(f"[{start}]  {chrom}:{start}-{start + window_size} ", end='')

        refwidth = end-start
        refseq = reference.fetch(chrom, start, start + refwidth)
        variants = list(truth_vcf.fetch(chrom, start, start + refwidth))

        # WONT ALWAYS WORK: Grab *ALL* variants and generate a single alt sequence with everything???
        pseudo_altseq = phaser.project_vars(variants, [np.argmax(v.samples[0]['GT']) for v in variants], refseq, start)
        pseudo_vars = list(vcf.aln_to_vars(refseq, pseudo_altseq, start))


        print(f" true: {len(pseudo_vars)}", end='')

        var_types = set()
        for true_var in pseudo_vars:
            vartype = util.var_type(true_var)
            var_types.add(vartype)
            # print(f"{true_var} ", end='')
            # print(f" hap0: {true_var in vars_hap0}, hap1: {true_var in vars_hap1}")
            if true_var in vars_hap0 or true_var in vars_hap1:
                tps.append(true_var)
                tot_tps += 1
                results[vartype]['tp'] += 1
                tp_varpos.append(true_var.pos - start)
            else:
                fns.append(true_var)
                tot_fns += 1
                results[vartype]['fn'] += 1
        print(f" {', '.join(var_types)} TP: {len(tps)} FN: {len(fns)}", end='')

        for var0 in vars_hap0:
            vartype = util.var_type(var0)
            if var0 not in pseudo_vars:
                fps.append(var0)
                fp_varpos.append(var0.pos - start)
                tot_fps += 1
                results[vartype]['fp'] += 1
        for var1 in vars_hap1:
            vartype = util.var_type(var1)
            if var1 not in pseudo_vars and var1 not in vars_hap0:
                fps.append(var1)
                fp_varpos.append(var1.pos - start)
                tot_fps += 1
                results[vartype]['fp'] += 1


        tp_pos = ", ".join(str(s) for s in tp_varpos)
        fp_pos = ", ".join(str(s) for s in fp_varpos[0:10])
        print(f" FP: {len(fps)}\t[{tp_pos}]  [{fp_pos}]")

    for key, val in results.items():
        print(f"{key} : total entries: {sum(val.values())}")
        for t, count in val.items():
            print(f"\t{t} : {count}")


def print_pileup(path, idx, target=None, **kwargs):
    path = Path(path)

    suffix = path.name.split("_")[-1]
    tgtpath = path.parent / f"tgt_{suffix}"
    if tgtpath.exists():
        tgt = util.tensor_from_file(tgtpath, device='cpu')
        logger.info(f"Found target file: {tgtpath}, loaded tensor of shape {tgt.shape}")
        for i in range(tgt.shape[1]):
            t = tgt[idx, i, :]
            bases = util.tgt_str(tgt[idx, i, :])
            print(bases)
    else:
        logger.info(f"No tgt file found (look for {tgtpath})")

    src = util.tensor_from_file(path, device='cpu')
    logger.info(f"Loaded tensor with shape {src.shape}")
    s = util.to_pileup(src[idx, :, :, :])
    print(s)


def alphanumeric_no_spaces(name):
    if re.match(r"[a-zA-Z0-9_-]+", name):
        return name
    else:
        raise argparse.ArgumentTypeError(f"{name} is not an alphanumeric plus '_' or '-' without spaces")


def main():
    parser = argparse.ArgumentParser()
    subparser = parser.add_subparsers()

    genparser = subparser.add_parser("pregen", help="Pre-generate tensors from BAMs")
    genparser.add_argument("-c", "--config", help="Training configuration yaml", required=True)
    genparser.add_argument("-d", "--dir", help="Output directory", default=".")
    genparser.add_argument("-s", "--sim", help="Generate simulated data", action='store_true')
    genparser.add_argument("-b", "--batch-size", help="Number of pileups to include in a single file (basically the batch size)", default=64, type=int)
    genparser.add_argument("-n", "--start-from", help="Start numbering from here", type=int, default=0)
    genparser.add_argument("-t", "--threads", help="Number of processes to use", type=int, default=1)
    # genparser.add_argument("-vpc", "--vals-per-class", help="The number of instances for each variant class in a label file; it will be set automatically if not specified", type=int, default=1000)
    genparser.add_argument("-mf", "--metadata-file", help="The metadata file that records each row in the encoded tensor files and the variant from which that row is derived. The name pregen_{time}.csv will be used if not specified.")
    genparser.set_defaults(func=pregen)

    printpileupparser = subparser.add_parser("print", help="Print a tensor pileup")
    printpileupparser.add_argument("-p", "--path", help="Path to saved tensor data", required=True)
    printpileupparser.add_argument("-i", "--idx", help="Index of item in batch to emit", required=True, type=int)
    printpileupparser.set_defaults(func=print_pileup)

    evalbamparser = subparser.add_parser("evalbam", help="Evaluate a BAM with labels")
    evalbamparser.add_argument("-c", "--config", help="Training configuration yaml", required=True)
    evalbamparser.add_argument("-m", "--statedict", help="Stored model", required=True)
    evalbamparser.add_argument("-b", "--bam", help="Input BAM file", required=True)
    evalbamparser.add_argument("-v", "--truth-vcf", help="Truth VCF", required=True)
    evalbamparser.add_argument("-l", "--labels", help="CSV file with truth variants", required=True)
    evalbamparser.set_defaults(func=eval_labeled_bam)

    trainparser = subparser.add_parser("train", help="Train a model")
    trainparser.add_argument("-n", "--epochs", type=int, help="Number of epochs to train for", default=100)
    trainparser.add_argument("-i", "--input-model", help="Start with parameters from given state dict")
    trainparser.add_argument("-o", "--output-model", help="Save trained state dict here", required=True)
    trainparser.add_argument("-ch", "--checkpoint-freq", help="Save model checkpoints frequency (0 to disable)", default=10, type=int)
    trainparser.add_argument("-lr", "--learning-rate", help="Initial learning rate", default=0.001, type=float)
    trainparser.add_argument("-c", "--config", help="Training configuration yaml", required=True)
    trainparser.add_argument("-d", "--datadir", help="Pregenerated data dir", default=None)
    trainparser.add_argument("-vd", "--val-dir", help="Pregenerated data for validation", default=None)
    trainparser.add_argument("-t", "--threads", help="Max number of threads to use for decompression (torch may use more)", default=4, type=int)
    trainparser.add_argument("-md", "--max-decomp-batches",
                             help="Max number batches to decompress and store in memory at once", default=4, type=int)
    trainparser.add_argument("-b", "--batch-size", help="The batch size, default is 64", type=int, default=64)
    trainparser.add_argument("-da", "--data-augmentation", help="Specify data augmentation options: 'shortening', 'shuffling', 'downsampling'. You can provide multiple options. Default is None", nargs="+", default=None)
    trainparser.add_argument("-fa", "--fraction-to-augment", help="Fraction of sample batches to augment. Needed with '--data-augmentation' option. Default is 0.25", default=0.25, type=float)
    trainparser.add_argument("-rn", "--wandb-run-name", type=alphanumeric_no_spaces, default=None,
                             help="Weights & Biases run name, must be alphanumeric plus '_' or '-'")
    trainparser.add_argument("--wandb-notes", type=str, default=None,
                             help="Weights & Biases run notes, longer description of run (like 'git commit -m')")
    trainparser.add_argument("--loss", help="Loss function to use, use 'ce' for CrossEntropy or 'sw' for Smith-Waterman", choices=['ce', 'sw'], default='ce')
    trainparser.set_defaults(func=train)
    callparser = subparser.add_parser("call", help="Call variants")
    callparser.add_argument("-m", "--model-path", help="Stored model", required=True)
    callparser.add_argument("-r", "--reference-fasta", help="Path to Fasta reference genome", required=True)
    callparser.add_argument("-b", "--bam", help="Input BAM file", required=True)
    callparser.add_argument("-d", "--bed", help="bed file defining regions to call", required=True)
    callparser.add_argument("-v", "--vcf-out", help="Output vcf file", required=True)
    callparser.set_defaults(func=call)

    args = parser.parse_args()
    args.cl_args = vars(args).copy()  # command line copy for logging
    args.func(**vars(args))


if __name__ == "__main__":
    main()

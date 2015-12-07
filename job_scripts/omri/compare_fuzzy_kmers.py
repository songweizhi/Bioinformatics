
import argparse
import os
import sys
import math
import time
from mypyli import jellyfish
import numpy as np
import logging
import re
from Bio import SeqIO
import queue
import multiprocessing
import psutil
#import matplotlib.pyplot as plt

#from matplotlib import rcParams
#rcParams.update({'figure.autolayout': True})


logging.basicConfig()
LOG = logging.getLogger(__name__)
LOG.setLevel("DEBUG")

"""

IDEA: I wonder if threads might be faster than multiprocessing because there is no need to pickle things when I can just use the shared memory.

TODO: Multithread uniqueness finding portion.

ISSUE: Generating the properly spaced amplicon list takes way too much memory for closely related genomes. (Over 100G for the Methylos)
    
    I think this is because each properly spaced pair is put in a directional hash

    The problem is the sheer number of potential amplicons. The number is well over 100million unique amplicons. 
    This value seems to be after processing 215 of 1757 partitions.


TODO: Print some final stats about amplicons (number unique, etc).

TODO: It would be great to be able to be able to load the indexes from a previous run to avoid having to recalculate them.
"""


class KmerCounter(object):

    # set max mem to 38G in mbytes
    MAX_MEM = 60 * 2 ** 10

    COMPLEMENT_MAP = {"A": "T", "T": "A", "G": "C", "C": "G"}

    def __init__(self, mismatches=1, stable_start=2, stable_end=2):
        self.mismatches = mismatches

        # stables are # bases at beginning and end that cannot be variable
        self.stable_start = stable_start
        self.stable_end = stable_end

        # make partitions based on stable_start
        self.queues = {}
        self.partitions = {}
        self.results_queue = multiprocessing.Queue()
        for kmer in self._recurse_all_kmers(self.stable_start):
            self.queues[kmer] = multiprocessing.Queue()
            self.partitions[kmer] = KmerPartition(kmer, self.queues[kmer], self.results_queue)
            self.partitions[kmer].start()

        # set genome id to starting value
        self.genome_id = 0

    @classmethod
    def _recurse_all_kmers(cls, k, curseq="", collected_seqs=[]):
        """ Generates all kmers to avoid having to store a large array """
        if len(curseq) == k:
            yield curseq
        else:
            for nt in ["A", "C", "G", "T"]:
                for kmer in cls._recurse_all_kmers(k, curseq + nt, collected_seqs):
                    yield kmer
 
    @classmethod
    def _reverse_complement(cls, kmer):
        return ''.join([cls.COMPLEMENT_MAP[nt] for nt in reversed(kmer)])


    def count_kmers(self, fasta_f, k):
        LOG.info("Counting {}-mers for {}".format(k, fasta_f))

        fasta_name = os.path.splitext(os.path.basename(fasta_f))[0]
        if os.path.isfile(fasta_name + ".counts.txt"):
            LOG.info("Found existing counts file for {}".format(fasta_name))
            kmer_counts = fasta_name + ".counts.txt"

        else:
            # count kmers using jellyfish
            jf_indx = jellyfish.count_kmers(fasta_f, k, prefix=fasta_name)
            kmer_counts = jellyfish.dump_kmer_counts(jf_indx, output_f=fasta_name + ".counts.txt")

        # read in the counts file
        num_processed = 0
        with open(kmer_counts, 'r') as IN:
            for line in IN:
                kmer, count = line.rstrip().split("\t")

                # send the kmer to the correct partition for processing
                self.partitions[kmer[:self.stable_start]].add_kmer(kmer, count)
                
                
                num_processed += 1
                #if num_processed == 100000:
                    #sys.exit()

        [print((par.name, len(par.kmers))) for par in self.partitions.values()]

        conserved_kmers = []
        for par in self.partitions.values():
            print((par.name, len(par.kmers)))
            par.update_kmer_file()
            conserved_kmers += par.get_conserved_kmers()

    def count_kmers_custom_single(self, fasta_f, k):
    
        self.genome_id += 1
        genome_str = str(self.genome_id)

        kmers_counted = 0

        contig_id = 0
        for seq in self._parse_fasta(fasta_f):
            contig_id += 1
            gen_ctg_str = ";".join((genome_str, str(contig_id)))

            # count kmers in a sliding window
            # must add 1 to the length because range has an exclusive end
            for indx in range(len(seq)+1 - k):
                kmers_counted += 1
                kmer = seq[indx:indx+k]

                # check if there is an N in the kmer and discard it if so
                if "N" in kmer:
                    continue
                
                self.partitions[kmer[:self.stable_start]].add_kmer(kmer, ";".join((gen_ctg_str, str(indx))))

                if kmers_counted % 10000 == 0:
                    print(kmers_counted)

        for partition in self.partitions.values():
            partition.update_kmer_file()



    def count_kmers_custom(self, fasta_f, k):
        """ Count kmers and their reverse complements """
    
        self.genome_id += 1
        genome_str = str(self.genome_id)

        kmers_counted = 0

        contig_id = 0
        for seq in self._parse_fasta(fasta_f):
            contig_id += 1
            contig_str = str(contig_id)
            #gen_ctg_str = ";".join((genome_str, str(contig_id)))

            # count kmers in a sliding window
            # must add 1 to the length because range has an exclusive end
            for indx in range(len(seq)+1 - k):
                kmers_counted += 1
                kmer = seq[indx:indx+k]

                # check if there is an N in the kmer and discard it if so
                if "N" in kmer:
                    continue
               

                # choose the < of the kmer and its RC
                canonical = sorted((kmer, self._reverse_complement(kmer)))[0]
        
                # check if the rc was used and reindex if not
                if canonical == kmer:
                    strand = "1"
            
                else:
                    strand = "-1"
 

                self.partitions[canonical[:self.stable_start]].kmer_queue.put((canonical, ";".join((genome_str, contig_str, str(indx), strand))))

                if kmers_counted % 10000 == 0:
                    # check if the queues are getting too big, or memory usage too high
                    for p in self.partitions.values():
                        if p.kmer_queue.qsize() > 1000:
                            time.sleep(2)

                    # check memory usage
                    main = psutil.Process(os.getpid())

                    # get mem of main process in MB
                    total_mem = main.get_memory_info()[0] / 2 ** 20
                    for partition in self.partitions.values():
                        ps = psutil.Process(partition.pid)
                        total_mem += ps.get_memory_info()[0] / 2 ** 20

                    # do an emergency dump if we are at 60% of max memory
                    # 60% because the writing to file step can add an extra 4% current mem per proc (16)
                    # and we don't want to exceed max
                    if total_mem >= .6 * self.MAX_MEM:
                        LOG.info("Memory usage = {}G... doing emergency dump.".format(total_mem / 2 ** 10))
                        for p in self.partitions.values():
                            p.kmer_queue.put("DUMP")
                    #print(kmers_counted)

        for p in self.partitions.values():
            p.kmer_queue.put("DUMP")


    def get_conserved_kmers_single(self, num_genomes):
        conserved_kmers = []
        for p in self.partitions.values():
            conserved_kmers += p.get_conserved_kmers()

        return conserved_kmers

    def get_conserved_kmers(self, num_genomes):
        for p in self.partitions.values():
            p.kmer_queue.put("CONSERVED{}".format(num_genomes))

        active_jobs = [p for p in self.partitions.values()]
        while active_jobs:
            try:
                yield(self.results_queue.get(False))
            except queue.Empty:
                new_active_jobs = []
                for job in active_jobs:
                    if job.is_alive():
                        new_active_jobs.append(job)
                    else:
                        #job.kmer_queue.join_thread()
                        job.join()
                        job.kmer_queue.close()

                active_jobs = new_active_jobs
                time.sleep(5)

        LOG.debug("All threads joined!")

        self.results_queue.close()


    def _parse_fasta(self, fasta_f):
        """ Yields sequences from a fasta file """

        with open(fasta_f, 'r') as IN:
            for record in SeqIO.parse(IN, 'fasta'):
                yield str(record.seq)


class KmerPartition(multiprocessing.Process):
    """ A free-standing class (for multithreading purposes) that processes a subset of kmers 
    
    Responsible for holding kmer information and dumping it to a file when it gets the signal.

    I think the best way to hold kmer information is in a trie. 
    """

    # binary here is merely idealistic because the binary gets converted to ints
    #encode_map = {"A": 0b00 "a": 0b00, "C": 0b01, "c": 0b01, "G": 0b10, "g": 0b10, "T": 0b11, "t":0b11}
    encode_map = {"A": "00", "C": "01", "G": "10", "T": "11"}


    def __init__(self, name, kmer_queue, results_queue):
        super().__init__()
        self.name = name
        self.kmer_queue = kmer_queue
        self.results_queue = results_queue

        # set up the storage file
        self.out_dir = "."
        self.file = "/".join([self.out_dir, self.name + "_data_dump.txt"])
        
        if not os.path.isfile(self.file):
            with open(self.file, 'w'):
                pass

        # set up some vars to use later
        self.kmers = {}
    
        self.mismatches = 1
        self.stable_start = 2
        self.stable_end = 2
        self.k = 20


    def run(self):
        LOG.debug("Starting thread {}".format(self.name)) 
        # wait for kmers to count
        while True:
            itm = self.kmer_queue.get()

            if type(itm) is tuple:
                self.add_kmer(kmer=itm[0], position=itm[1])

            else:

                if itm == "DUMP":
                    self.expand_and_update_kmer_file()

                elif itm.startswith("CONSERVED"):
                    # split the number of genomes from the itm
                    self.get_conserved_kmers(int(itm.split("D")[1]))
                    return

        return

    @classmethod
    def _recurse_all_groups(cls, k, curseq="", mismatches=0):
        """ Generates all kmer groups for a given length k """
        if len(curseq) == k:
            yield curseq

        # don't allow N's in stable regions
        elif len(curseq) <= self.stable_start:
            pass

            for nt in ["N", "A", "C", "G", "T"]:
                for kmer in cls._recurse_all_kmers(k, curseq + nt):
                    yield kmer
 
    def _digest_kmer(self, kmer, current_seq="", mismatches=0):
        """ This is a slow point of my program right now. I need to speed up. """
        # check if we are to the end
        if len(current_seq) >= self.k - self.stable_end:
            # not sure why I can't just return rather than yield, then return
            # might have something to do with the fact that some will be yielded and others will be returned
            yield "".join((current_seq, kmer))
            return
        
        # skip if we are still in the stable start
        elif len(current_seq) < self.stable_start:
            pass

        else:
            
            # check if the current base can be fuzzy (not too many previous errors)
            if mismatches < self.mismatches:
                # run the ambiguous result
                for result in self._digest_kmer(kmer[1:], current_seq+"N", mismatches+1):
                    yield result
                    break
 
            # base cannot be fuzzy, already enough errors
            else:
                # once again, not sure why I can't just return this...
                yield "".join((current_seq, kmer))
                return
        
        # run the next iteration if not returned
        for result in self._digest_kmer(kmer[1:], current_seq+kmer[0], mismatches):
            #print(("result", result))
            yield result


    def add_kmer(self, kmer, position):

        for group in self._digest_kmer(kmer):
            try:
                self.kmers[group].append(position)
            except KeyError:
                self.kmers[group] = [position]


    ###
    # Checking analyzing stored files
    ###
    def get_conserved_kmers(self, num_genomes):
        """ Look for kmers that have a position from each genomes. """
       
        LOG.debug("Finding conserved kmers for {}...".format(self.name))
        conserved_kmers = []
        genomes_not_found = {}
        with open(self.file, 'r') as IN:
            for line in IN:
                elems = line[:-1].split("\t")
                kmer = elems[0]
                locations = elems[1:]
    
                # let's assume that most kmers will NOT be found in all genomes
                # so, I don't want to make objects (slow) until I know they are conserved. 
                # this will be slower for kmers that ARE in all genomes
    
                # first check if there are enough locations -- this will remove most kmers
                if len(locations) >= num_genomes:
                    
                    # now check that each genome is represented and build a location list
                    genomes_found = [False]*num_genomes
                    for location in locations:

                        try:
                            genome = int(location.split(";")[0]) - 1
                        except:
                            print(locations)
                            raise
                        try:
                            genomes_found[genome].append(location)
                        except AttributeError:
                            genomes_found[genome] = [location]
                        
                    not_found = []
                    for indx, pos in enumerate(genomes_found):
                        if pos is False:
                            not_found.append(indx)
                    
      
                    if not_found:
                        # build a histogram like data structure with kmers not found
                        try:
                            genomes_not_found[len(not_found)].append(kmer)
                        except KeyError:
                            genomes_not_found[len(not_found)] = [kmer]
                    else:
                        # if all genomes found, convert location into object and add to list of conserved regions
                        loc_objs = []
                        for location in locations:
                            genome, contig, start, strand = location.split(";")
                            loc_objs.append(Location(genome, contig, start, strand))
                        conserved_kmers.append((kmer, loc_objs))

        LOG.debug("Found {} conserved kmers - {}".format(len(conserved_kmers), self.name))
        for kmer in conserved_kmers:
            self.results_queue.put(kmer)
        return conserved_kmers


    def expand_and_update_kmer_file(self):
        """ Loads/creates a kmer file and writes all the current kmer information to it. """

        LOG.debug("Updating kmer file for {}".format(self.file))

        tmp_file = self.file + "_tmp"

        # iterate through the input file and stored kmers and dump ordered results
        with open(self.file, 'r') as IN, open(tmp_file, 'w') as OUT:

            stored_line = IN.readline()[:-1]
           

            for kmer in sorted(self.kmers):
                # make sure something is on the line; assume file over if not
                if stored_line:
                    #stored_kmer = int(stored_line.split("\t")[0])
                    stored_kmer = stored_line.split("\t")[0]
                else:
                    line = "\t".join([str(kmer), "\t".join(self.kmers[kmer])])
                    OUT.write(line + "\n")
                    continue

                
                if kmer < stored_kmer:      # stored_kmer comes after this one, add new line
                    # write kmer and all locations
                    line = "\t".join([str(kmer),"\t".join(self.kmers[kmer])])

                elif kmer == stored_kmer:   # stored_kmer is this one, append to line
                    line = "\t".join([stored_line, "\t".join(self.kmers[kmer])])
                    stored_line = IN.readline()[:-1]

                else:   # stored_kmer is less than this one; need to read some more lines
                    # read in some more lines until the line is greater than 
                    line = stored_line
                    while kmer > stored_kmer:
                        stored_line = IN.readline()[:-1]

                        # check for the end of the file and break if found
                        if not stored_line:
                            break

                        OUT.write(line + "\n")
                        stored_kmer = stored_line.split("\t")[0]

                        if kmer == stored_kmer:
                            line = "\t".join([stored_line, "\t".join(self.kmers[kmer])])
                            stored_line = IN.readline()[:-1]
                            # while loop will break at the end of this
                        elif kmer < stored_kmer:    # we have overshot, add the kmer from memory
                            line = "\t".join([str(kmer), "\t".join(self.kmers[kmer])])
                            # while loop will break at the end of this

                        else:   # there is still more looping over file needed to be done
                            line = stored_line


                OUT.write(line + "\n")

        os.rename(tmp_file, self.file)
        self.kmers = {}


class Amplicon(object):

    existing_amplicons = {}    
    amplicon_index = 0

    def __init__(self, name):
        self.name = name
        self.locations = []

        Amplicon.amplicon_index += 1
        self.index = Amplicon.amplicon_index

    @classmethod
    def from_kmer_pair(cls, k1, loc1, k2, loc2):
        """ 
        Collapse each kmer pair into a canonical representation.

        """

        key = (k1, k2)
        try:
            cls.existing_amplicons[key].add_new_location(loc1, loc2)
        except KeyError:
            amp = cls("-".join(key))
            amp.add_new_location(loc1, loc2)
            cls.existing_amplicons[key] = amp

    def add_new_location(self, loc1, loc2):
       self.locations.append((loc1, loc2))


class Location(object):
    """
    A location of a kmer indexed by genome, contig, start, and strand 
    
    Uses slots to be a lightweight struct.

    Fun fact, this is actually smaller than a tuple with the same info (this takes 72 bytes, tup is 80) according to sys.getsizeof()
    """
    #__slots__ = ["genome", "contig", "start", "strand"]

    def __init__(self, genome, contig, start, strand):
        self.genome = int(genome)
        self.contig = int(contig)
        self.start = int(start)
        self.strand = int(strand)

#
## Find kmers that are the right distance apart
#
def get_properly_spaced_pairs(conserved_kmers, k=20, max_dist=400, min_dist=100):

    # build partitions by contig because we know that to be the right distance, it must be on the same contig
    # this just reformats the data for a quicker searching algorithm
    ppp = {}
    for kmer, locations in conserved_kmers:
        for location in locations:
            
            try:
                ppp[(location.genome, location.contig)].append((kmer, location))
            except KeyError:
                ppp[(location.genome, location.contig)] = [(kmer, location)]

    # this part could be split up over multiple processes
    LOG.info("Processing {} conserved kmer partitions...".format(len(ppp)))
    results_queue = multiprocessing.Queue()
    bins_processed = 1
    for data_bin in ppp.values():
        LOG.debug("Processing bin {}...".format(bins_processed))
        bins_processed += 1


        for itm in _find_properly_spaced(results_queue, data_bin, k, max_dist, min_dist):
            yield itm
"""
    while True:
        if results_queue.qsize() > 0:
            yield results_queue.get()

        else:
            break
"""

def _find_properly_spaced(queue, data_bin, k=20, max_dist=400, min_dist=100):
    """ 
    Must look for the proper location.

    Must also be on the same strand???
    I think that's true but I should think about it more.
    """
    # sort kmers by position
    sorted_kmers = sorted(data_bin, key=lambda k: k[1].start)

    #print("# kmers: {}".format(len(sorted_kmers)))

    num_properly_spaced = 0
    for indx1, k1 in enumerate(sorted_kmers):
        # begin at the next kmer
        indx2 = indx1 + 1
        while indx2 < len(sorted_kmers):
            k2 = sorted_kmers[indx2]
            indx2 += 1

            # Find distance between start of k2 and the end of k1 (interprimer distance)
            # locations are starting values so need to add k to k1 to get the end of it
            # We know k2 is always >= k1 because we sorted it
            distance = k2[1].start - (k1[1].start + k) 

            # break at first one that fails max dist test
            if distance > max_dist:
                break

            # skip if the distance is too small
            elif distance < min_dist:
                continue

            # distance must be acceptable
            else:

                # make sure they are on the same strand
                if k1[1].strand == k2[1].strand:

                    # puts a tuple(first, second) of tuple(kmer, location)
                    #queue.put((k1, k2))
                    num_properly_spaced += 1
                    yield (k1, k2)

    LOG.debug("Found {} properly spaced pairs.".format(num_properly_spaced))
    

def write_all_amplicons(amplicons, fastas, k, out_dir):
    """ 
    TODO: Need to do something with kmers that are duplicated in a genome.

    Maybe it would be best to take it into account in the stats section...

    Also need to figure out why RC isn't working correctly. All strands ate +1
    """

    if os.path.isdir(out_dir):
        raise ValueError("{} already exists, cowardly refusing to overwrite.".format(out_dir))
    else:
        os.mkdir(out_dir)

   
    #
    ## make a hierarchial list of amplicons to get to read each input FASTA once
    #
    LOG.info("Making sequence shopping list...")
    num_amplicons = 0
    shopping_list = {}
    for amplicon in amplicons.values():
        # make sure the amplicon represents more than one genome
        # this is a pre-check for adding to the shopping list
        genomes_represented = set()
        for location in amplicon.locations:
            genomes_represented.add(location[0].genome)

        if len(genomes_represented) == 1:
            continue

       
        
        num_amplicons += 1
        for location in amplicon.locations:
            first, second = location

            # there has GOT to be a better way to do this...
            try:
                shopping_list[first.genome][first.contig][(first, second)].append(amplicon)
            except KeyError:
                try:
                    shopping_list[first.genome][first.contig][(first, second)] = [amplicon]
                except KeyError:
                    try:
                        shopping_list[first.genome][first.contig] = {(first, second): [amplicon]}
                    except KeyError:
                        shopping_list[first.genome] = {first.contig: {(first, second): [amplicon]}}

    LOG.info("Writting {} amplicon files...".format(num_amplicons))
    written_files = []
    for g_indx, fasta in enumerate(fastas, 1):
        if g_indx in shopping_list:
            with open(fasta) as IN:
                fasta_name = os.path.splitext(os.path.basename(fasta))[0]

                ### this part is getting down to a single region in the genome
                for c_indx, record in enumerate(SeqIO.parse(IN, "fasta"), 1):
                    if c_indx in shopping_list[g_indx]:
                        for location_pair in shopping_list[g_indx][c_indx]:
                            first, second = location_pair

                            # the current algorithm for finding amplicons ensures that position of first is always <= second, however, if it isn't the seq gotten from indexing will be blank (silently) so I want to make sure first is always before second
                            assert(first.start < second.start)
                            
                            # reverse complement if on rev strand
                            if first.strand == 1:
                                new_rec = record[first.start:second.start+k]
                            else:
                                new_rec = record[first.start:second.start+k].reverse_complement()

                            new_rec.id = "genome={f.genome};contig={f.contig};location=[{f.start}:{end}];strand={f.strand}".format(f=first, end=second.start + k)
                            new_rec.description = ""



                            ### this part is writing the region to each amplicon it is a part of
                            for amp in shopping_list[g_indx][c_indx][location_pair]:
                                out_path = "{}/{}_{}.fasta".format(out_dir, amp.index, amp.name)
                                written_files.append(out_path)
                                with open(out_path, 'a') as OUT:
                                    SeqIO.write(new_rec, OUT, "fasta")

    return written_files

def get_amplicon_stats(amplicon_fastas, input_fastas, k):
    
    LOG.info("Calculating Amplicon stats...")

    # make a list of fasta names to be used in the matrix
    input_names = [os.path.splitext(os.path.basename(f))[0] for f in input_fastas]

    amplicon_stats = {}
    for fasta_f in amplicon_fastas:
        fasta_name = os.path.splitext(os.path.basename(fasta_f))[0]
        amplicon_stats[fasta_name] = {}

        # get the number of Ns from the name
        # fasta name will have the format $ampindx_$fwdprim-$revprim
        primers = fasta_name.split("_")[1]
        fwd_primer, rev_primer = primers.split("-")
       
        amplicon_stats[fasta_name]["fwd_N"] = str(fwd_primer.count("N"))
        amplicon_stats[fasta_name]["rev_N"] = str(rev_primer.count("N"))


        #
        ## check which seqs can be told apart
        #
        seqs = {}
        with open(fasta_f, 'r') as IN:
            for record in SeqIO.parse(IN, "fasta"):
                m = re.match(".*genome=(?P<genome>[^;]+).*strand=(?P<strand>[^;]+)", record.description)
                genome = m.group("genome")
                strand = m.group("strand")
                
                # store the amplified region (rc if necessary) 
                if strand == "1":
                    seq = record.seq[k:-k]
                else:
                    seq = record.seq[k:-k].reverse_complement()
                
                try:
                    seqs[genome].append(seq)
                except KeyError:
                    seqs[genome] = [seq]

        

        # Seqs can have multiple locations in a single amplicon -- flatten these
        flattened_seqs = {}
        for genome in seqs:
            for indx, seq in enumerate(seqs[genome]):
                flattened_seqs[(genome, indx)] = seq
        

        # Assign cluster for each seq
        cluster = 0
        cluster_counts = {}
        clusters = {}
        for seq1 in flattened_seqs:

            # skip seqs that already have cluster assigned
            if seq1 in clusters:
                continue
            else:
                cluster += 1
                clusters[seq1] = str(cluster)
                cluster_counts[cluster] = 1

            for seq2 in flattened_seqs:
                if seq1 is seq2:
                    continue

                # skip seqs that have already been processed
                elif seq2 in clusters:
                    continue

                else:
                    # check if the seqs are == 
                    if flattened_seqs[seq1] == flattened_seqs[seq2]:
                        clusters[seq2] = str(cluster)
                        cluster_counts[cluster] += 1

       
        # group clusters back by genomes
        duplicated_genomes = 0
        unique_genomes = set()     
        genome_clusters = {}
        for key in clusters:
            genome, index = key

            cluster = clusters[key]

            if cluster_counts[int(cluster)] == 1:
                unique_genomes.add(genome)
            try:
                genome_clusters[genome].append(clusters[key])
                duplicated_genomes += 1
            except KeyError:
                genome_clusters[genome] = [clusters[key]]
           
        amplicon_stats[fasta_name]["num_genomes"] = len(genome_clusters)
        amplicon_stats[fasta_name]["num_unique"] = len(unique_genomes)
        amplicon_stats[fasta_name]["num_duplicates"] = duplicated_genomes
        amplicon_stats[fasta_name]["genome_clusters"] = genome_clusters

    # sort the amplicons by num unique_genomes number (descending) and number of N's(ascending), and then num duplicates
    sorted_amplicons = sorted(amplicon_stats, key=lambda k: (-1 * amplicon_stats[k]["num_unique"], amplicon_stats[k]["fwd_N"] + amplicon_stats[k]["rev_N"], amplicon_stats[k]["num_duplicates"]))


    with open("amplicon_stats.txt", 'w') as STATS, open("amplicon_matrix.txt", 'w') as MATR:
        # write headers to both files
        STATS.write("\t".join(("amplicon", "num_genomes", "num_unique", "num_duplicates", "fwd_N", "rev_N")) + "\n")
        MATR.write("\t".join(["amplicon"] + input_names) + "\n")

        for amp in sorted_amplicons:
            write_stats = [amp] + [str(amplicon_stats[amp][k]) for k in ["num_genomes", "num_unique", "num_duplicates", "fwd_N", "rev_N"]]
            write_matr = [amp]
            for name in input_names:
                try:
                    write_matr.append(";".join(amplicon_stats[amp]["genome_clusters"][name]))
                except KeyError:
                    write_matr.append("0")

            STATS.write("\t".join(write_stats) + "\n")
            MATR.write("\t".join(write_matr) + "\n")


def get_memory_usage():
    """ Returns the memory usage of this process and all children """
    # check memory usage
    main = psutil.Process(os.getpid())

    total_mem = main.get_memory_info()[0] / 2 ** 30
    for child in main.get_children():
        total_mem += child.get_memory_info()[0] / 2 ** 30

    return total_mem

def main(fastas, k):

    kcounter = KmerCounter()
   
    # count kmers
    for fasta in fastas:
        LOG.info("Indexing k-mers in {}".format(fasta))
        #kcounter.count_kmers_custom(fasta, 20)
        #pass

    # map the conserved kmers back to genomes
    conserved_kmers = []
    for kmer in kcounter.get_conserved_kmers(len(fastas)):
        conserved_kmers.append(kmer)

    LOG.info("Found {} fuzzy kmers conserved in all genomes.".format(len(conserved_kmers)))

    # send kcounter for garbage collection
    del kcounter


    LOG.debug("Memory after kmer counting = {}G".format(get_memory_usage()))

    #
    ## Generate all properly spaced (same contig, strand, and within some distance) conserved kmer pairs
    ## Put the properly spaced pairs in a data structure that doesn't care the order of the pairing (i.e. what strand the pair is found on per genome
    #
    LOG.info("Generating properly spaced amplicons...")
    for pair in get_properly_spaced_pairs(conserved_kmers):
        start, end = pair

        s_kmer, s_loc = start
        e_kmer, e_loc = end

        Amplicon.from_kmer_pair(s_kmer, s_loc, e_kmer, e_loc)

        if len(Amplicon.existing_amplicons) % 100000 == 0:
            LOG.debug("Memory after adding {} unique amplicons = {}G".format(len(Amplicon.existing_amplicons), get_memory_usage()))

    print("Properly spaced amplicons for one or more genomes = {}".format(len(Amplicon.existing_amplicons)))

    
    written_files = write_all_amplicons(Amplicon.existing_amplicons, fastas, k, "amplicon_fastas")
    
    get_amplicon_stats(written_files, fastas, k)


    """
    All that is left to do is to make a table of which genomes have unique sequences for each amplicon.

    I'm thinking there will be multiple files ideally. 

    1. Amplicon stats - sorted file that has # of Ns in fwd and rev primers and the number of genomes it can tell appart

    2. Amp X genome matrix - matrix that tells which genomes each amplicon can tell apart

    3. Fasta file for each amplicon that can tell apart any number of genomes uniquely (n > 1).
    """


    

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-fastas", help="a bunch of fastas to compare", nargs="+", required=True)
    parser.add_argument("-k", help="value of k to use", required=True, type=int)
    args = parser.parse_args()

    main(args.fastas, args.k)

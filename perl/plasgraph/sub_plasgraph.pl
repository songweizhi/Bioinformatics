#!usr/bin/perl

use strict;
use warnings; 


#I need to change the scope of the vars or the structure of the program if I want this sub version to work.  

####
####Setup and error handling
####
####


#Check and intake arguments for source dir and output dir
if (@ARGV != 2) {
    die("Usage: plasgraph.pl <source directory> <output directory> \n");
}
my ($src_directory, $out_directory) = @ARGV;


&setup;


####
####Begin loop to process each file
####

my $file_n = 1;
foreach my $file (@files) {
    #update user on progress also counts non-fasta files...
    print("Processing file ",  $file_n++, " of ", scalar @files, "... \n");

    &cBar;

    &graph_r;

    &plasmids_only;

	close($FASFILE);
}	


#Print out the files within the directory that were determined not to be fasta files. 
print("\n\nThe following files were not processed through cBar because they were determined not to be fasta files:\n");
print(join("\n", @not_fasta), "\n");

#remove temp file
unlink("$out_directory/tempplasmids.txt");



########
# Subs #
########

#checks for correct input and prepares to process files
sub setup
{
    #try to open the source directory
    opendir(my $DIR, $src_directory) or die "Directory $src_directory not found.\n";


    #store all file names to an array and close directory
    my @files = readdir $DIR;
    my @not_fasta;
    closedir $DIR;

    #check if output directory exists, if not make it
    #could use to put this in positive conditional
    unless(-d $out_directory) {
        mkdir($out_directory) or die "Output directory $out_directory could not be created. \n";
        print("Directory created \n");
    }
}




sub cBar {
        ###check if fasta format by looking for ">" in header
        open(my $FASFILE, "<", "$src_directory/$file");
        my $header = readline($FASFILE);

        #if the file is a fasta file proceed
        if (defined($header) and substr($header, 0, 1) eq ">") {   
            #format output file by removing extension
            (my $noext = $file) =~ s{\.[^.]+$}{};

            #check if log has already been created, if so skip the file. 
            if (-e "$out_directory/cBar_logs/$noext.cBar.txt") {
                next;
            }
                                    
            unless(-d "$out_directory/cBar_logs") {i
                mkdir("$out_directory/cBar_logs") or die("Directory cBar_logs could not be created");
            }
            #use this call rather than system because it stops output from cBar
            `perl cBar.1.2/cBar.pl $src_directory/$file $out_directory/cBar_logs/$noext.cBar.txt`;
    }

    else {
        push(@not_fasta, "$src_directory/$file");
    }
}



sub graph_r {
    #set up directory for graphs
    unless(-d "$out_directory/graphs") {
        mkdir("$out_directory/graphs") or die "Directory Graphs could not be made";
    }

    #call R with the input file, file name, and output file
    `Rscript pgraph.r $out_directory/cBar_logs/$noext.cBar.txt $noext $out_directory/graphs/`;
}


sub plasmids_only {
    #make fasta of plasmids only
    unless(-d "$out_directory/plasmids_only") {
    mkdir("$out_directory/plasmids_only") or die("Directory plasmids_only could not be made.");
    }
                                                            

    `grep "Plasmid" $out_directory/cBar_logs/$noext.cBar.txt | awk '{print \$1}' > $out_directory/tempplasmids.txt`;
                                                                                ##call get seqs by header
                                                                                `perl fastX_get_seqs_by_id.pl --fastx_file $src_directory/$file --type fasta --names_file $out_directory/tempplasmids.txt --out_prefix $out_directory/plasmids_only/$noext\_onlyplasmids`;

}




    

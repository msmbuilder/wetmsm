"""
Using assignments, write data to the User field in vmd.

This involves writing a data file and a TCL script to get VMD
to load in the data file.
"""

import os
import logging

import numpy as np
import mcmd
import tables

from ._vmd_write import _compute_chunk_add, _compute_chunk_max, \
    _compute_chunk_avg


log = logging.getLogger()

VMDSCRIPT = """
# Load in molecule
set mol [mol new {traj_fn} step {step} waitfor all]
mol addfile {top_fn} waitfor all

# Open data file
set sel [atomselect $mol all]
set nf [molinfo $mol get numframes]
set fp [open {dat_fn} r]
set line ""

# Each line of the data file corresponds to a frame
for {{set i 0}} {{$i < $nf}} {{incr i}} {{
  gets $fp line
  $sel frame $i
  $sel set user $line
}}

close $fp
$sel delete

# For convenience, set up representations as well

mol delrep 0 top

mol representation NewCartoon 0.3 10.0 4.1 0
mol color ColorID 4
mol selection {{protein}}
mol addrep top
mol smoothrep top 0 5


mol representation CPK 1.0 0.2 10.0 10.0
mol color User
mol selection {{user > 1}}
mol addrep top
mol selupdate 1 top 1
mol colupdate 1 top 1
"""


def _compute(assn, loading2d, n_frames, n_atoms, solvent_ind, chunksize=1000000,
             which='add'):
    """Add "loading" to each relevant atom

    :param assn: (M,4) array 'assignments' file
        The columns are: frame, solvent, solute, shell (indices)

    :param loading2d: Values to apply to relevant features

    :param n_frames: Number of frames. Needed to initialize the array
    :param n_atoms: Number of atoms. Needed to initialize the array

    :param solvent_ind: Indices of solvent atoms among all the atoms
        instead of whatever indexing is used in `assn`

    :param which: {add, max, avg}

    :param chunksize: How many rows to read at once.


    :returns user: (n_frames, n_atoms) array of values. Write this
        to a file that can be loaded into VMD.

    """

    # Initialize
    user = np.zeros((n_frames, n_atoms))

    func_map = {'add': _compute_chunk_add, 'max': _compute_chunk_max,
                'avg': _compute_chunk_avg}
    compute_chunk = func_map[which]

    # Deal with chunks of the pytables EARRAY
    n_chunks = assn.shape[0] // chunksize + 1

    for chunk_i in range(n_chunks):
        chunk = assn.read(chunksize * chunk_i, chunksize * (chunk_i + 1))
        log.debug("Chunk %d: %s", chunk_i, str(chunk.shape))

        compute_chunk(chunk, solvent_ind, loading2d, user)
        del chunk

    return user


class VMDWriter(object):
    """Write VMD scripts to load tICA loadings into 'user' field.

    :param assn: (M,4) array 'assignments' file
        The columns are: frame, solvent, solute, shell (indices)
    :param n_frames: Number of frames. Needed to initialize the array
    :param n_atoms: Number of all atoms. Needed to initialize the array

    :param solvent_ind: Indices of solvent atoms among all the atoms
        instead of whatever indexing is used in `assn`

    :param n_solute: Number of solute atoms for translating from 2d to 3d
    :param n_shells: Number of solvent shells. This is needed so we can
        back out the correct shape of the fingerprint vector
    """

    def __init__(self, assn, solvent_ind, n_frames, n_atoms, n_solute,
                 n_shells):
        self.assn = assn
        self.solvent_ind = solvent_ind

        self.to3d = None
        self.to2d = None

        self.n_frames = n_frames
        self.n_solute = n_solute
        self.n_shells = n_shells
        self.n_atoms = n_atoms
        self.which = 'add'


    def compute(self, loading, deleted):
        """Assign loadings to atoms based on an assignments file.

        :param loading: 1-d loadings (from tICA/PCA) which we apply
            to relevant atoms

        :param deleted: Indices (in 1d) of features that were removed
            (likely due to low-variance) before performing tICA
        """

        user = _compute(self.assn, self.translate_loading(loading, deleted),
                        self.n_frames, self.n_atoms, self.solvent_ind,
                        which=self.which)

        return user

    def set_up_translation(self, deleted):
        """Turn indices from one form to another ('2d' -- '3d')

        :param deleted: Indices of states that were pruned

        :returns to3d, to2d: Dictionaries
        """
        to3d = {}
        to2d = {}

        absi = 0  # Absolute index
        pruni = 0  # Pruned index
        for ute in range(self.n_solute):
            for sh in range(self.n_shells):
                if not np.in1d(absi, deleted):
                    to3d[pruni] = (ute, sh)
                    to2d[(ute, sh)] = pruni
                    pruni += 1
                else:
                    to2d[(ute, sh)] = -1
                absi += 1

        self.to3d = to3d
        self.to2d = to2d
        return to3d, to2d

    def translate_loading(self, loading, deleted):
        """Take 1-dim `loading` from tICA/PCA and expand to (solute, shell)
        indexing.

        :param loading: 1-d loadings (from tICA/PCA) which we apply
            to relevant atoms

        :param deleted: Indices (in 1d) of features that were removed
            (likely due to low-variance) before performing tICA
        """
        loading2d = np.zeros((self.n_solute, self.n_shells))

        absi = 0
        pruni = 0
        for ute in range(self.n_solute):
            for sh in range(self.n_shells):
                if not np.in1d(absi, deleted):
                    loading2d[ute, sh] = loading[pruni]
                    pruni += 1
                else:
                    loading2d[ute, sh] = 0.0

                absi += 1

        return loading2d

    def write_dat(self, data, features_to_select, out_fn_base, traj_fn=None,
                  top_fn=None, stride=1):

        # TODO: Get rid of this

        dat_out_fn = "{}.dat".format(out_fn_base)
        tcl_out_fn = "{}.tcl".format(out_fn_base)

        howmany = self.n_frames // stride

        with open(dat_out_fn, 'w') as dat_f:
            for i, row in enumerate(
                    self.compute(data, features_to_select, stride)):
                [dat_f.write('{} '.format(d)) for d in row]
                dat_f.write('\n')

                if i % 10 == 0:
                    log.info("Done %d / %d", i, howmany)
                else:
                    log.debug("Done %d / %d", i, howmany)

        if traj_fn is not None and top_fn is not None:
            with open(tcl_out_fn, 'w') as tcl_f:
                tcl_f.write(VMDSCRIPT.format(top_fn=top_fn, traj_fn=traj_fn,
                                             step=stride,
                                             dat_fn=os.path.basename(
                                                 dat_out_fn)))


class VMDWriterCommand(mcmd.Parsable):
    # TODO: This command is incomplete
    # Do it by-hand in IPython for now

    def __init__(self, assn_fn='assign.h5',
                 solvent_ind_fn='solvent_indices.dat',
                 pruned_fn='fp2d_deleted.pickl', loading_data='.pickl',
                 dat_out_fn='trj.dat'):
        pass


    def main(self):
        assn_h = tables.open_file(self.assn_fn)
        assn = assn_h.root.assignments

        solute_ind = None
        solvent_ind = None


def parse():
    vc = mcmd.parsify(VMDWriterCommand)
    vc.main()


if __name__ == "__main__":
    log.addHandler(logging.StreamHandler())
    log.setLevel(logging.INFO)
    parse()


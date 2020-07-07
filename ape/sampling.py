# -*- coding: utf-8 -*-

# [1] https://doi.org/10.1021/acs.jctc.5b01177 Thermodynamics of Anharmonic Systems: Uncoupled Mode Approximations for Molecules

import os
import csv
import logging
import numpy as np
from time import gmtime, strftime

import rmgpy.constants as constants

from arkane.common import symbol_by_number
from arkane.statmech import is_linear

from arc.species.species import ARCSpecies

from ape.qchem import QChemLog
from ape.torsion import HinderedRotor 
from ape.common import SolvEig, mass_weighted_hessian, sampling_along_torsion, sampling_along_vibration
from ape.InternalCoordinates import get_RedundantCoords, getXYZ
from ape.exceptions import InputError


class SamplingJob(object):
    """
    The SamplingJob class.
    """

    def __init__(self, label=None, input_file=None, output_directory=None, protocol=None, multiplicity=None, charge = None,\
    level_of_theory=None, basis=None, ncpus=None, is_ts=None):
        self.input_file = input_file
        self.label = label
        self.output_directory = output_directory
        self.protocol = protocol
        self.multiplicity = multiplicity
        self.charge = charge
        self.level_of_theory = level_of_theory
        self.basis = basis
        self.ncpus = ncpus
        self.is_ts = is_ts

    def parse(self):
        Log = QChemLog(self.input_file)
        self.hessian = Log.load_force_constant_matrix()
        coordinates, number, mass = Log.load_geometry()
        self.conformer, unscaled_frequencies = Log.load_conformer()
        if self.protocol is None:
            self.protocol = 'UMVT'
        if self.multiplicity is None:
            self.multiplicity = Log.multiplicity
            # self.multiplicity = self.ARCSpecies.multiplicity
        if self.charge is None:
            self.charge = Log.charge
            # self.charge = 0
        
        # A $rem variable is needed when moledule is radical
        self.unrestricted = Log.is_unrestricted()

        self.is_QM_MM_INTERFACE = Log.is_QM_MM_INTERFACE()
        if self.is_QM_MM_INTERFACE:
            self.QM_ATOMS = Log.get_QM_ATOMS()
            self.number_of_fixed_atoms = Log.get_number_of_atoms() - len(Log.get_QM_ATOMS())
            self.ISOTOPES = Log.get_ISOTOPES()
            self.nHcap = len(self.ISOTOPES)
            self.force_field_params = Log.get_force_field_params()
            self.opt = Log.get_opt()
            self.fixed_molecule_string = Log.get_fixed_molecule()
            self.QM_USER_CONNECT = Log.get_QM_USER_CONNECT()
            self.QM_mass = Log.QM_mass
            self.QM_coord = Log.QM_coord
            self.natom =  len(self.QM_ATOMS) + len(self.ISOTOPES)
            self.symbols = Log.QM_atom
            self.cart_coords = self.QM_coord.reshape(-1,)
            self.conformer.coordinates = (self.QM_coord, "angstroms")
            self.conformer.mass = (self.QM_mass, "amu")
            xyz = ''
            for i in range(len(self.QM_ATOMS)):
                if self.QM_USER_CONNECT[i].endswith('0  0  0  0'):
                    xyz += '{}\t{}\t\t{}\t\t{}'.format(self.symbols[i],self.cart_coords[3*i],self.cart_coords[3*i+1],self.cart_coords[3*i+2])
                    if i != self.natom-1: xyz += '\n'
            self.xyz = xyz
            if self.xyz == '':
                self.ARCSpecies = None
            else:
                self.ARCSpecies = ARCSpecies(label=self.label, xyz=self.xyz)
            if self.ncpus is None:
                self.ncpus = 8 # default cpu for QM/MM calculation
            self.zpe = Log.load_zero_point_energy()
        else:
            self.natom = Log.get_number_of_atoms()
            self.symbols = [symbol_by_number[i] for i in number]
            self.cart_coords = coordinates.reshape(-1,)
            self.conformer.coordinates = (coordinates, "angstroms")
            self.conformer.number = number
            self.conformer.mass = (mass, "amu")            
            self.xyz = getXYZ(self.symbols, self.cart_coords)
            self.ARCSpecies = ARCSpecies(label=self.label, xyz=self.xyz)
            if self.ncpus is None:
                self.ncpus = self.ARCSpecies.number_of_heavy_atoms
                if self.ncpus > 8: self.ncpus = 8
            self.zpe = Log.load_zero_point_energy()

        # Determine whether or not the species is linear from its 3D coordinates
        self.linearity = is_linear(self.conformer.coordinates.value)

        # Determine hindered rotors information
        if self.protocol == 'UMVT':
            self.rotors_dict = self.get_rotors_dict()
            self.n_rotors = len(self.rotors_dict)
        else:
            self.rotors_dict = []
            self.n_rotors = 0

        if self.is_QM_MM_INTERFACE:
            self.nmode = 3 * len(Log.get_QM_ATOMS()) - (1 if self.is_ts else 0)
            self.n_vib = 3 * len(Log.get_QM_ATOMS()) - self.n_rotors - (1 if self.is_ts else 0)
        else:        
            self.nmode = 3 * self.natom - (5 if self.linearity else 6) - (1 if self.is_ts else 0)
            self.n_vib = 3 * self.natom - (5 if self.linearity else 6) - self.n_rotors - (1 if self.is_ts else 0)

        # Create RedundantCoords object
        self.internal = get_RedundantCoords(self.symbols, self.cart_coords, self.rotors_dict)
        if self.is_QM_MM_INTERFACE:
            self.internal.nHcap = self.nHcap
        
        # Extract imaginary frequency from transition state
        if self.is_ts:
            self.imaginary_frequency = Log.load_negative_frequency()
            
    def get_rotors_dict(self):
        rotors_dict = {}
        species = self.ARCSpecies
        if species is None:
            return rotors_dict
        species.determine_rotors()
        for i in species.rotors_dict:
            rotors_dict[i+1] = {}
            pivots = species.rotors_dict[i]['pivots']
            top = species.rotors_dict[i]['top']
            scan = species.rotors_dict[i]['scan']
            rotors_dict[i+1]['pivots'] = pivots 
            rotors_dict[i+1]['top'] = top
            rotors_dict[i+1]['scan'] = scan
        return rotors_dict

    def sampling(self, thresh=0.05, save_result=True, scan_res=10):
        xyz_dict = {}
        energy_dict = {}
        mode_dict = {}
        if not os.path.exists(self.output_directory):
            os.makedirs(self.output_directory)
        path = os.path.join(self.output_directory, 'output_file', self.label)
        if not os.path.exists(path):
            os.makedirs(path)
        if self.protocol == 'UMVT' and self.n_rotors != 0:
            n_vib = self.n_vib
            if self.is_QM_MM_INTERFACE:
                # Due to frustrated translation and rotation
                n_vib -= 6
            rotor = HinderedRotor(symbols=self.symbols, conformer=self.conformer, hessian=self.hessian, rotors_dict=self.rotors_dict, linear=self.linearity, is_ts=self.is_ts, n_vib=n_vib)
            ph = rotor.projectd_hessian()
            mwph = mass_weighted_hessian(self.conformer, ph, linear=self.linearity, is_ts=self.is_ts)
            vib_freq, unweighted_v = SolvEig(mwph, self.conformer.mass.value_si, self.n_vib)
            logging.debug('\nFrequencies(cm-1) from projected Hessian: {}'.format(vib_freq))
            
            for i in range(self.n_rotors):
                mode = i+1
                if self.is_QM_MM_INTERFACE:
                    XyzDictOfEachMode, EnergyDictOfEachMode, ModeDictOfEachMode, min_elect = sampling_along_torsion(self.symbols, self.cart_coords, mode, self.internal, self.conformer, rotor, \
                    self.rotors_dict, scan_res, path, thresh, self.ncpus, self.charge, self.multiplicity, self.level_of_theory, self.basis, self.unrestricted, self.is_QM_MM_INTERFACE, self.nHcap, \
                    self.QM_USER_CONNECT, self.QM_ATOMS, self.force_field_params, self.fixed_molecule_string, self.opt, self.number_of_fixed_atoms)
                else:
                    XyzDictOfEachMode, EnergyDictOfEachMode, ModeDictOfEachMode, min_elect = sampling_along_torsion(self.symbols, self.cart_coords, mode, self.internal, self.conformer, rotor, \
                    self.rotors_dict, scan_res, path, thresh, self.ncpus, self.charge, self.multiplicity, self.level_of_theory, self.basis, self.unrestricted)
                xyz_dict[mode] = XyzDictOfEachMode
                energy_dict[mode] = EnergyDictOfEachMode
                mode_dict[mode] = ModeDictOfEachMode
        
        elif self.protocol == 'UMN' or self.n_rotors == 0:
            mwh = mass_weighted_hessian(self.conformer, self.hessian, linear=self.linearity, is_ts=self.is_ts)
            vib_freq, unweighted_v = SolvEig(mwh, self.conformer.mass.value_si, self.n_vib)
            logging.debug('\nVibrational frequencies of normal modes: {}'.format(vib_freq))

        for i in range(self.nmode):
            if i in range(self.n_rotors): continue
            mode = i+1
            vector=unweighted_v[i-self.n_rotors]
            freq = vib_freq[i-self.n_rotors]
            magnitude = np.linalg.norm(vector)
            reduced_mass = magnitude ** -2 / constants.amu # in amu
            step_size = np.sqrt(constants.hbar / (reduced_mass * constants.amu) / (freq * 2 * np.pi * constants.c * 100)) * 10 ** 10 # in angstrom
            normalizes_vector = vector/magnitude
            qj = np.matmul(self.internal.B, normalizes_vector)
            P = np.ones(self.internal.B.shape[0], dtype=int)
            n_rotors = len(self.rotors_dict)
            if n_rotors != 0:
                P[-n_rotors:] = 0
            P = np.diag(P)
            qj = P.dot(qj).reshape(-1,)
            if self.is_QM_MM_INTERFACE:
                XyzDictOfEachMode, EnergyDictOfEachMode, ModeDictOfEachMode, min_elect = sampling_along_vibration(self.symbols, self.cart_coords, mode, self.internal, qj, freq, reduced_mass, \
                step_size, path, thresh, self.ncpus, self.charge, self.multiplicity, self.level_of_theory, self.basis, self.unrestricted, self.is_QM_MM_INTERFACE, self.nHcap, self.QM_USER_CONNECT, \
                self.QM_ATOMS, self.force_field_params, self.fixed_molecule_string, self.opt, self.number_of_fixed_atoms)
            else:
                XyzDictOfEachMode, EnergyDictOfEachMode, ModeDictOfEachMode, min_elect = sampling_along_vibration(self.symbols, self.cart_coords, mode, self.internal, qj, freq, reduced_mass, step_size, \
                path, thresh, self.ncpus, self.charge, self.multiplicity, self.level_of_theory, self.basis, self.unrestricted)
            xyz_dict[mode] = XyzDictOfEachMode
            energy_dict[mode] = EnergyDictOfEachMode
            mode_dict[mode] = ModeDictOfEachMode

        # add the ground-state energy (including zero-point energy) of the conformer
        self.e_elect = min_elect # in Hartree/particle
        e0 = self.e_elect * constants.E_h * constants.Na + self.zpe # in J/mol
        self.conformer.E0 = (e0, "J/mol")

        if save_result:
            if os.path.exists(self.csv_path):
                os.remove(self.csv_path)
            self.write_samping_result_to_csv_file(self.csv_path, mode_dict, energy_dict)

            path = os.path.join(self.output_directory, 'plot', self.label)
            if not os.path.exists(path):
                os.makedirs(path)
            self.write_sampling_displaced_geometries(path, energy_dict, xyz_dict)

        return xyz_dict, energy_dict, mode_dict

    def write_samping_result_to_csv_file(self, csv_path, mode_dict, energy_dict):
        write_min_elect = False
        if os.path.exists(csv_path) is False:
            write_min_elect = True

        with open(csv_path, 'a') as f:
            writer = csv.writer(f)
            if write_min_elect:
                writer.writerow(['min_elect', self.e_elect])
            for mode in mode_dict.keys():
                if mode_dict[mode]['mode'] == 'tors':
                    is_tors = True
                    name = 'mode_{}_tors'.format(mode)
                else:
                    is_tors = False
                    name = 'mode_{}_vib'.format(mode)
                writer.writerow([name])
                if is_tors:
                    writer.writerow(['symmetry_number', mode_dict[mode]['symmetry_number']])
                writer.writerow(['M', mode_dict[mode]['M']])
                writer.writerow(['K', mode_dict[mode]['K']])
                writer.writerow(['step_size', mode_dict[mode]['step_size']])
                writer.writerow(['sample', 'total energy(HARTREE)'])
                for sample in sorted(energy_dict[mode].keys()):
                    writer.writerow([sample, energy_dict[mode][sample]])
            f.close()
            # logging.debug('Have saved the sampling result in {path}'.format(path=csv_path))
    
    def write_sampling_displaced_geometries(self, path, energy_dict, xyz_dict):
        # creat a format can be read by VMD software
        for mode in energy_dict.keys():
            txt_path = os.path.join(path, 'mode_{}.txt'.format(mode))
            with open(txt_path, 'w') as f:
                for sample in sorted(energy_dict[mode].keys()): 
                    content = record_script.format(natom=self.natom, sample=sample, e_elect=energy_dict[mode][sample], xyz=xyz_dict[mode][sample])
                    f.write(content)
                current_time = strftime("%Y-%m-%d %H:%M:%S", gmtime())
                f.write('\n    This sampling was finished on:   {time}'.format(time=current_time))
                f.write("""\n=------------------------------------------------------------------------------=""")
                f.write("""\nSampling finished.""")
                f.write("""\n=------------------------------------------------------------------------------=""")
                f.close()

    def execute(self):
        """
        Execute APE.
        """
        self.csv_path = os.path.join(self.output_directory, '{}_samping_result.csv'.format(self.label))
        if os.path.exists(self.csv_path):
            os.remove(self.csv_path)
        self.parse()
        self.sampling()

# creat a format can be read by VMD software
record_script ='''{natom}
# Point {sample} Energy = {e_elect}
{xyz}
'''
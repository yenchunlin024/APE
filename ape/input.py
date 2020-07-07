# -*- coding: utf-8 -*-

"""
This module contains functionality for parsing APE input files.
"""


import logging
import os.path

import numpy as np

from rmgpy.exceptions import InputError
from rmgpy.kinetics.model import TunnelingModel
from rmgpy.kinetics.tunneling import Wigner, Eckart
from rmgpy.reaction import Reaction
from rmgpy.species import Species, TransitionState
from rmgpy.statmech.conformer import Conformer
from rmgpy.statmech.rotation import LinearRotor, NonlinearRotor, KRotor, SphericalTopRotor
from rmgpy.statmech.torsion import HinderedRotor, FreeRotor
from rmgpy.statmech.translation import IdealGasTranslation
from rmgpy.statmech.vibration import HarmonicOscillator

from arkane.kinetics import KineticsJob

from ape.sampling import SamplingJob
from ape.qchem import QChemLog
from ape.thermo import ThermoJob

################################################################################

species_dict, transition_state_dict, reaction_dict = dict(), dict(), dict()
job_list = list()
directory, output_directory = str(), str()


def species(label, *args, **kwargs):
    """Load a species from an input file"""
    global species_dict, job_list, directory
    if label in species_dict:
        raise ValueError('Multiple occurrences of species with label {0!r}.'.format(label))
    logging.info('Loading species {0}...'.format(label))

    spec = Species(label=label)
    species_dict[label] = spec

    path = None
    if len(args) == 1:
        # The argument is a path to a conformer input file
        path = os.path.join(directory, args[0])
        job = SamplingJob(label=label, input_file=path, output_directory=output_directory)
        spec.conformer, unscaled_frequencies = QChemLog(path).load_conformer()
        logging.debug('Added species {0} to a sampling job.'.format(label))
        job_list.append(job)
    elif len(args) > 1:
        raise InputError('species {0} can only have two non-keyword argument '
                         'which should be the species label and the '
                         'path to a quantum file.'.format(spec.label))

    if len(kwargs) > 0:
        # The species parameters are given explicitly
        protocol = 'UMVT'
        multiplicity = None
        charge = None
        for key, value in kwargs.items():
            if key == 'protocol':
                protocol = value.upper()
            elif key == 'multiplicity':
                multiplicity = value
            elif key == 'charge':
                charge = value
            else:
                raise TypeError('species() got an unexpected keyword argument {0!r}.'.format(key))
               
        job.protocol = protocol
        job.multiplicity = multiplicity
        job.charge = charge
    
    return spec

def transitionState(label, *args, **kwargs):
    """Load a transition state from an input file"""
    global transition_state_dict, job_list, directory
    if label in transition_state_dict:
        raise ValueError('Multiple occurrences of transition state with label {0!r}.'.format(label))
    logging.info('Loading transition state {0}...'.format(label))
    ts = TransitionState(label=label)
    transition_state_dict[label] = ts

    if len(args) == 1:
        # The argument is a path to a conformer input file
        path = os.path.join(directory, args[0])
        job = SamplingJob(label=label, input_file=path, output_directory=output_directory, is_ts=True)
        Log = QChemLog(path)
        ts.conformer, unscaled_frequencies = Log.load_conformer()
        ts.frequency = (Log.load_negative_frequency(), "cm^-1")
        job_list.append(job)

    elif len(args) == 0:
        # The species parameters are given explicitly
        E0 = None
        modes = []
        spin_multiplicity = 1
        optical_isomers = 1
        frequency = None
        for key, value in kwargs.items():
            if key == 'E0':
                E0 = value
            elif key == 'modes':
                modes = value
            elif key == 'spinMultiplicity':
                spin_multiplicity = value
            elif key == 'opticalIsomers':
                optical_isomers = value
            elif key == 'frequency':
                frequency = value
            else:
                raise TypeError('transition_state() got an unexpected keyword argument {0!r}.'.format(key))

        ts.conformer = Conformer(E0=E0, modes=modes, spin_multiplicity=spin_multiplicity,
                                 optical_isomers=optical_isomers)
        ts.frequency = frequency
    else:
        if len(args) == 0 and len(kwargs) == 0:
            raise InputError(
                'The transition_state needs to reference a quantum job file or contain kinetic information.')
        raise InputError('The transition_state can only link a quantum job or directly input information, not both.')

    if len(kwargs) > 0:
        # The species parameters are given explicitly
        protocol = 'UMVT'
        for key, value in kwargs.items():
            if key == 'protocol':
                protocol = value.upper()
            else:
                raise TypeError('species() got an unexpected keyword argument {0!r}.'.format(key))
               
        job.protocol = protocol

    return ts

def reaction(label, reactants, products, transitionState=None, kinetics=None, tunneling=''):
    """Load a reaction from an input file"""
    global reaction_dict, species_dict, transition_state_dict
    if label in reaction_dict:
        label = label + transitionState
        if label in reaction_dict:
            raise ValueError('Multiple occurrences of reaction with label {0!r}.'.format(label))
    logging.info('Loading reaction {0}...'.format(label))
    reactants = sorted([species_dict[spec] for spec in reactants])
    products = sorted([species_dict[spec] for spec in products])
    if transitionState:
        transitionState = transition_state_dict[transitionState]
    if transitionState and (tunneling == '' or tunneling is None):
        transitionState.tunneling = None
    elif tunneling.lower() == 'wigner':
        transitionState.tunneling = Wigner(frequency=None)
    elif tunneling.lower() == 'eckart':
        transitionState.tunneling = Eckart(frequency=None, E0_reac=None, E0_TS=None, E0_prod=None)

    elif transitionState and not isinstance(tunneling, TunnelingModel):
        raise ValueError('Unknown tunneling model {0!r}.'.format(tunneling))
    rxn = Reaction(label=label, reactants=reactants, products=products, transition_state=transitionState,
                   kinetics=kinetics)

    if isinstance(rxn, Reaction):
        reaction_dict[label] = rxn

    return rxn

def thermo(label, Tlist=[298.15]):
    """Generate a thermo job"""
    global job_list, species_dict
    try:
        spec = species_dict[label]
    except KeyError:
        raise ValueError('Unknown species label {0!r} for thermo() job.'.format(label))
    for job in job_list:
        if job.label == label:
            input_file = job.input_file
    job = ThermoJob(label=label, input_file= input_file, output_directory=output_directory, Tlist=Tlist)
    job_list.append(job)

def kinetics(label, Tmin=None, Tmax=None, Tlist=None, Tcount=0, sensitivity_conditions=None, three_params=True):
    """Generate a kinetics job"""
    global job_list, reaction_dict
    try:
        rxn = reaction_dict[label]
    except KeyError:
        raise ValueError('Unknown reaction label {0!r} for kinetics() job.'.format(label))
    job = KineticsJob(reaction=rxn, Tmin=Tmin, Tmax=Tmax, Tcount=Tcount, Tlist=Tlist,
                      sensitivity_conditions=sensitivity_conditions, three_params=three_params)
    job_list.append(job)


################################################################################


def load_input_file(path, output_path=None):
    """
    Load the APE input file located at `path` on disk, and return a list of
    the jobs defined in that file.
    """
    global species_dict, transition_state_dict, reaction_dict, job_list, directory, output_directory
    directory = os.path.dirname(path)
    output_directory = output_path
    # Clear module-level variables
    species_dict, transition_state_dict, reaction_dict = dict(), dict(), dict()
    job_list = []

    global_context = {'__builtins__': None}
    local_context = {
        '__builtins__': None,
        'True': True,
        'False': False,
        'range': range,
        # Statistical mechanics
        'IdealGasTranslation': IdealGasTranslation,
        'LinearRotor': LinearRotor,
        'NonlinearRotor': NonlinearRotor,
        'KRotor': KRotor,
        'SphericalTopRotor': SphericalTopRotor,
        'HarmonicOscillator': HarmonicOscillator,
        'HinderedRotor': HinderedRotor,
        'FreeRotor': FreeRotor,
        # Functions
        'reaction': reaction,
        'species': species,
        'transitionState': transitionState,
        # Jobs
        'kinetics': kinetics,
        'thermo': thermo,
    }

    with open(path, 'r') as f:
        try:
            exec(f.read(), global_context, local_context)
        except (NameError, TypeError, SyntaxError):
            logging.error('The input file {0!r} was invalid:'.format(path))
            raise
    
    level_of_theory = local_context.get('level_of_theory', None)
    basis = local_context.get('basis', None)

    for job in job_list:
        if isinstance(job, SamplingJob):
            pass

        #if isinstance(job, KineticsJob):
        #    job.path = os.path.join(directory, job.path)

    return job_list, reaction_dict, species_dict, transition_state_dict



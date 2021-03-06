"""The base experiment class."""

from collections import Counter
from functools import wraps
import imp
import inspect
import logging
from operator import itemgetter
import os
import random
import requests
import sys
import time
import uuid

from sqlalchemy import and_

from dallinger.config import get_config, LOCAL_CONFIG
from dallinger.data import Data
from dallinger.data import export
from dallinger.data import is_registered
from dallinger.data import load as data_load
from dallinger.models import Network, Node, Info, Transformation, Participant
from dallinger.heroku.tools import HerokuApp
from dallinger.information import Gene, Meme, State
from dallinger.nodes import Agent, Source, Environment
from dallinger.transformations import Compression, Response
from dallinger.transformations import Mutation, Replication
from dallinger.networks import Empty

logger = logging.getLogger(__file__)


def exp_class_working_dir(meth):
    @wraps(meth)
    def new_meth(self, *args, **kwargs):
        try:
            config = get_config()
            orig_path = os.getcwd()
            new_path = os.path.dirname(
                sys.modules[self.__class__.__module__].__file__
            )
            os.chdir(new_path)
            # Override configs
            config.register_extra_parameters()
            config.load_from_file(LOCAL_CONFIG)
            return meth(self, *args, **kwargs)
        finally:
            config.clear()
            os.chdir(orig_path)
    return new_meth


class Experiment(object):
    """Define the structure of an experiment."""
    app_id = None
    # Optional Redis channel to create and subscribe to on launch. Note that if
    # you define a channel, you probably also want to override the send()
    # method, since this is where messages from Redis will be sent.
    channel = None
    exp_config = None

    def __init__(self, session=None):
        """Create the experiment class. Sets the default value of attributes."""

        #: Boolean, determines whether the experiment logs output when
        #: running. Default is True.
        self.verbose = True

        #: String, the name of the experiment. Default is "Experiment
        #: title".
        self.task = "Experiment title"

        #: session, the experiment's connection to the database.
        self.session = session

        #: int, the number of practice networks (see
        #: :attr:`~dallinger.models.Network.role`). Default is 0.
        self.practice_repeats = 0

        #: int, the number of non practice networks (see
        #: :attr:`~dallinger.models.Network.role`). Default is 0.
        self.experiment_repeats = 0

        #: int, the number of participants
        #: required to move from the waiting room to the experiment.
        #: Default is 0 (no waiting room).
        self.quorum = 0

        #: int, the number of participants
        #: requested when the experiment first starts. Default is 1.
        self.initial_recruitment_size = 1

        #: dictionary, the classes Dallinger can make in response
        #: to front-end requests. Experiments can add new classes to this
        #: dictionary.
        self.known_classes = {
            "Agent": Agent,
            "Compression": Compression,
            "Environment": Environment,
            "Gene": Gene,
            "Info": Info,
            "Meme": Meme,
            "Mutation": Mutation,
            "Node": Node,
            "Replication": Replication,
            "Response": Response,
            "Source": Source,
            "State": State,
            "Transformation": Transformation,
        }

        #: dictionary, the properties of this experiment that are exposed
        #: to the public over an AJAX call
        if not hasattr(self, 'public_properties'):
            # Guard against subclasses replacing this with a @property
            self.public_properties = {}

        if session:
            self.configure()

    def configure(self):
        """Load experiment configuration here"""
        pass

    @property
    def background_tasks(self):
        """An experiment may define functions or methods to be started as
        background tasks upon experiment launch.
        """
        return []

    @property
    def recruiter(self):
        """Recruiter, the Dallinger class that recruits participants.
        Default is HotAirRecruiter in debug mode and MTurkRecruiter in other modes.
        If recruiter param in config is set, there can be other recuiters. This
        last part could (should) be made pluggable.
        """
        from dallinger.recruiters import HotAirRecruiter
        from dallinger.recruiters import MTurkLargeRecruiter
        from dallinger.recruiters import MTurkRecruiter
        from dallinger.recruiters import BotRecruiter

        config = get_config()
        try:
            debug_mode = config.get('mode', None) == 'debug'
        except RuntimeError:
            # Config not yet loaded
            debug_mode = False

        recruiter = config.get('recruiter', None)
        if recruiter == 'bogus':
            # For forcing failures in tests
            raise NotImplementedError
        if debug_mode and recruiter != 'bots':
            return HotAirRecruiter
        if recruiter == 'bots':
            return BotRecruiter.from_current_config
        if recruiter == 'mturklarge':
            return MTurkLargeRecruiter.from_current_config
        return MTurkRecruiter.from_current_config

    def send(self, raw_message):
        """socket interface implementation, and point of entry for incoming
        Redis messages.

        param raw_message is a string with a channel prefix, for example:

            'shopping:{"type":"buy","color":"blue","quantity":"2"}'
        """
        pass

    def setup(self):
        """Create the networks if they don't already exist."""
        if not self.networks():
            for _ in range(self.practice_repeats):
                network = self.create_network()
                network.role = "practice"
                self.session.add(network)
            for _ in range(self.experiment_repeats):
                network = self.create_network()
                network.role = "experiment"
                self.session.add(network)
            self.session.commit()

    def create_network(self):
        """Return a new network."""
        return Empty()

    def networks(self, role="all", full="all"):
        """All the networks in the experiment."""
        if full not in ["all", True, False]:
            raise ValueError("full must be boolean or all, it cannot be {}"
                             .format(full))

        if full == "all":
            if role == "all":
                return Network.query.all()
            else:
                return Network\
                    .query\
                    .filter_by(role=role)\
                    .all()
        else:
            if role == "all":
                return Network.query.filter_by(full=full)\
                    .all()
            else:
                return Network\
                    .query\
                    .filter(and_(Network.role == role, Network.full == full))\
                    .all()

    def get_network_for_participant(self, participant):
        """Find a network for a participant.

        If no networks are available, None will be returned. By default
        participants can participate only once in each network and participants
        first complete networks with `role="practice"` before doing all other
        networks in a random order.

        """
        key = participant.id
        networks_with_space = Network.query.filter_by(
            full=False).order_by(Network.id).all()
        networks_participated_in = [
            node.network_id for node in
            Node.query.with_entities(Node.network_id)
                .filter_by(participant_id=participant.id).all()
        ]

        legal_networks = [
            net for net in networks_with_space
            if net.id not in networks_participated_in
        ]

        if not legal_networks:
            self.log("No networks available, returning None", key)
            return None

        self.log("{} networks out of {} available"
                 .format(len(legal_networks),
                         (self.practice_repeats + self.experiment_repeats)),
                 key)

        legal_practice_networks = [net for net in legal_networks
                                   if net.role == "practice"]
        if legal_practice_networks:
            chosen_network = legal_practice_networks[0]
            self.log("Practice networks available."
                     "Assigning participant to practice network {}."
                     .format(chosen_network.id), key)
        else:
            chosen_network = self.choose_network(legal_networks, participant)
            self.log("No practice networks available."
                     "Assigning participant to experiment network {}"
                     .format(chosen_network.id), key)
        return chosen_network

    def choose_network(self, networks, participant):
        return random.choice(networks)

    def create_node(self, participant, network):
        """Create a node for a participant."""
        return Node(network=network, participant=participant)

    def add_node_to_network(self, node, network):
        """Add a node to a network.

        This passes `node` to :func:`~dallinger.models.Network.add_node()`.

        """
        network.add_node(node)

    def data_check(self, participant):
        """Check that the data are acceptable.

        Return a boolean value indicating whether the `participant`'s data is
        acceptable. This is meant to check for missing or invalid data. This
        check will be run once the `participant` completes the experiment. By
        default performs no checks and returns True. See also,
        :func:`~dallinger.experiments.Experiment.attention_check`.

        """
        return True

    def bonus(self, participant):
        """The bonus to be awarded to the given participant.

        Return the value of the bonus to be paid to `participant`. By default
        returns 0.

        """
        return 0

    def bonus_reason(self):
        """The reason offered to the participant for giving the bonus.

        Return a string that will be included in an email sent to the
        `participant` receiving a bonus. By default it is "Thank you for
        participating! Here is your bonus."

        """
        return "Thank for participating! Here is your bonus."

    def attention_check(self, participant):
        """Check if participant performed adequately.

        Return a boolean value indicating whether the `participant`'s data is
        acceptable. This is mean to check the participant's data to determine
        that they paid attention. This check will run once the *participant*
        completes the experiment. By default performs no checks and returns
        True. See also :func:`~dallinger.experiments.Experiment.data_check`.

        """
        return True

    def submission_successful(self, participant):
        """Run when a participant submits successfully."""
        pass

    def recruit(self):
        """Recruit participants to the experiment as needed.

        This method runs whenever a participant successfully completes the
        experiment (participants who fail to finish successfully are
        automatically replaced). By default it recruits 1 participant at a time
        until all networks are full.

        """
        if not self.networks(full=False):
            self.log("All networks full: closing recruitment", "-----")
            self.recruiter().close_recruitment()

    def log(self, text, key="?????", force=False):
        """Print a string to the logs."""
        if force or self.verbose:
            print(">>>> {} {}".format(key, text))
            sys.stdout.flush()

    def log_summary(self):
        """Log a summary of all the participants' status codes."""
        participants = Participant.query\
            .with_entities(Participant.status).all()
        counts = Counter([p.status for p in participants])
        sorted_counts = sorted(counts.items(), key=itemgetter(0))
        self.log("Status summary: {}".format(str(sorted_counts)))
        return sorted_counts

    def save(self, *objects):
        """Add all the objects to the session and commit them.

        This only needs to be done for networks and participants.

        """
        if len(objects) > 0:
            self.session.add_all(objects)
        self.session.commit()

    def node_post_request(self, participant, node):
        """Run when a request to make a node is complete."""
        pass

    def node_get_request(self, node=None, nodes=None):
        """Run when a request to get nodes is complete."""
        pass

    def vector_post_request(self, node, vectors):
        """Run when a request to connect is complete."""
        pass

    def vector_get_request(self, node, vectors):
        """Run when a request to get vectors is complete."""
        pass

    def info_post_request(self, node, info):
        """Run when a request to create an info is complete."""
        pass

    def info_get_request(self, node, infos):
        """Run when a request to get infos is complete."""
        pass

    def transmission_post_request(self, node, transmissions):
        """Run when a request to transmit is complete."""
        pass

    def transmission_get_request(self, node, transmissions):
        """Run when a request to get transmissions is complete."""
        pass

    def transformation_post_request(self, node, transformation):
        """Run when a request to transform an info is complete."""
        pass

    def transformation_get_request(self, node, transformations):
        """Run when a request to get transformations is complete."""
        pass

    def fail_participant(self, participant):
        """Fail all the nodes of a participant."""
        participant_nodes = Node.query\
            .filter_by(participant_id=participant.id, failed=False)\
            .all()

        for node in participant_nodes:
            node.fail()

    def data_check_failed(self, participant):
        """What to do if a participant fails the data check.

        Runs when `participant` has failed
        :func:`~dallinger.experiments.Experiment.data_check`. By default calls
        :func:`~dallinger.experiments.Experiment.fail_participant`.

        """
        self.fail_participant(participant)

    def attention_check_failed(self, participant):
        """What to do if a participant fails the attention check.

        Runs when `participant` has failed the
        :func:`~dallinger.experiments.Experiment.attention_check`. By default calls
        :func:`~dallinger.experiments.Experiment.fail_participant`.

        """
        self.fail_participant(participant)

    def assignment_abandoned(self, participant):
        """What to do if a participant abandons the hit.

        This runs when a notification from AWS is received indicating that
        `participant` has run out of time. Calls
        :func:`~dallinger.experiments.Experiment.fail_participant`.

        """
        self.fail_participant(participant)

    def assignment_returned(self, participant):
        """What to do if a participant returns the hit.

        This runs when a notification from AWS is received indicating that
        `participant` has returned the experiment assignment. Calls
        :func:`~dallinger.experiments.Experiment.fail_participant`.

        """
        self.fail_participant(participant)

    def assignment_reassigned(self, participant):
        """What to do if the assignment assigned to a participant is
        reassigned to another participant while the first participant
        is still working.

        This runs when a participant is created with the same assignment_id
        as another participant if the earlier participant still has the status
        "working". Calls :func:`~dallinger.experiments.Experiment.fail_participant`.

        """
        self.fail_participant(participant)

    @exp_class_working_dir
    def run(self, exp_config=None, app_id=None, bot=False, **kwargs):
        """Deploy and run an experiment.

        The exp_config object is either a dictionary or a
        ``localconfig.LocalConfig`` object with parameters
        specific to the experiment run grouped by section.
        """
        import dallinger as dlgr

        if app_id is None:
            app_id = self.make_uuid()

        if bot:
            kwargs['recruiter'] = 'bots'

        self.app_id = app_id
        self.exp_config = exp_config or kwargs

        if self.exp_config.get("mode") == u"debug":
            dlgr.command_line.debug.callback(
                verbose=True,
                bot=bot,
                proxy=None,
                exp_config=self.exp_config
            )
        else:
            dlgr.command_line.deploy_sandbox_shared_setup(
                app=app_id,
                verbose=self.verbose,
                exp_config=self.exp_config
            )
        return self._finish_experiment()

    def collect(self, app_id, exp_config=None, bot=False, **kwargs):
        """Collect data for the provided experiment id.

        The ``app_id`` parameter must be a valid UUID.
        If an existing data file is found for the UUID it will
        be returned, otherwise - if the UUID is not already registered -
        the experiment will be run and data collected.

        See ``run`` method above for other parameters
        """
        try:
            orig_path = os.getcwd()
            new_path = os.path.dirname(
                sys.modules[self.__class__.__module__].__file__
            )
            os.chdir(new_path)
            results = data_load(app_id)
            self.log(u'Data found for experiment {}, retrieving.'.format(app_id),
                     key=u"Retrieve:")
            return results
        except IOError:
            self.log(
                u'Could not fetch data for id: {}, checking registry'.format(app_id),
                key=u"Retrieve:"
            )
        finally:
            os.chdir(orig_path)

        exp_config = exp_config or {}
        if is_registered(app_id):
            raise RuntimeError(u'The id {} is registered, '.format(app_id) +
                               u'but you do not have permission to access to the data')
        elif kwargs.get('mode') == u'debug' or exp_config.get('mode') == u'debug':
            raise RuntimeError(u'No remote or local data found for id {}'.format(app_id))

        try:
            assert isinstance(uuid.UUID(app_id, version=4), uuid.UUID)
        except (ValueError, AssertionError):
            raise ValueError('Invalid UUID supplied {}'.format(app_id))

        self.log(u'{} appears to be a new experiment id, running experiment.'.format(app_id),
                 key=u"Retrieve:")
        return self.run(exp_config, app_id, bot, **kwargs)

    @classmethod
    def make_uuid(cls):
        """Generate a new uuid."""
        return str(uuid.UUID(int=random.getrandbits(128)))

    def _finish_experiment(self):
        # Debug runs synchronously
        if self.exp_config.get('mode') != 'debug':
            self.log("Waiting for experiment to complete.", "")
            while self.experiment_completed() is False:
                time.sleep(30)
            self.end_experiment()
        return self.retrieve_data()

    def experiment_completed(self):
        """Checks the current state of the experiment to see whether it has
        completed"""
        heroku_app = HerokuApp(self.app_id)
        status_url = '/summary'.format(heroku_app.url)
        data = {}
        try:
            resp = requests.get(status_url)
            data = resp.json()
        except (ValueError, requests.exceptions.RequestException):
            logger.exception('Error fetching experiment status.')
        logger.debug('Current application state: {}'.format(data))
        return data.get('completed', False)

    def retrieve_data(self):
        """Retrieves and saves data from a running experiment"""
        local = False
        if self.exp_config.get('mode') == 'debug':
            local = True
        filename = export(self.app_id, local=local)
        logger.debug('Data exported to %s' % filename)
        return Data(filename)

    def end_experiment(self):
        """Terminates a running experiment"""
        HerokuApp(self.app_id).destroy()


def load():
    """Load the active experiment."""
    if os.getcwd() not in sys.path:
        sys.path.append(os.getcwd())

    try:
        exp = imp.load_source('dallinger_experiment', "dallinger_experiment.py")
        classes = inspect.getmembers(exp, inspect.isclass)
        exps = [c for c in classes
                if (c[1].__bases__[0].__name__ in "Experiment")]
        this_experiment = exps[0][0]
        mod = __import__('dallinger_experiment', fromlist=[this_experiment])
        return getattr(mod, this_experiment)

    except ImportError:
        logger.error('Could not import experiment.')
        raise

"""Experiment path and metadata management.

Call ``experiment_setup()`` (or ``experiment()``) once per session to set the
base path, sample name, and file base name.  The ``experiment`` singleton is
used by local scan plans to construct output file paths.
"""

__all__ = [
    "experiment",
    "experiment_setup",
    "experiment_change_sample",
]

from os import chdir
from pathlib import Path
from logging import getLogger

logger = getLogger(__name__)


class ExperimentClass:
    """Manages experiment paths and metadata for local scans.

    Attributes
    ----------
    base_experiment_path : Path or None
        Root directory for this experiment.
    sample : str or None
        Sample name; used as a sub-folder under ``base_experiment_path``.
    file_base_name : str or None
        Base name for output files (e.g. ``"scan"`` → ``scan_00001_master.hdf``).
    data_management : None
        Placeholder for DM metadata (always None at 6-ID-B).
    esaf : None
        Placeholder for ESAF metadata.
    proposal : None
        Placeholder for proposal metadata.
    """

    base_experiment_path = None
    sample = None
    file_base_name = None

    # Placeholders used by local_scans metadata blocks.
    data_management = None
    esaf = None
    proposal = None

    @property
    def experiment_path(self):
        """Full path: ``base_experiment_path / sample``."""
        if None in (self.base_experiment_path, self.sample):
            raise ValueError(
                "The base folder or sample name are not defined. "
                "Please run experiment_setup()."
            )
        return Path(self.base_experiment_path) / self.sample

    @experiment_path.setter
    def experiment_path(self, *args):
        raise AttributeError(
            "experiment_path is automatically generated from "
            "base_experiment_path and sample."
        )

    def __repr__(self):
        lines = ["\n-- Experiment setup --"]
        lines.append(f"Base path:  {self.base_experiment_path}")
        lines.append(f"Sample:     {self.sample}")
        lines.append(f"Base name:  {self.file_base_name}")
        try:
            lines.append(f"Full path:  {self.experiment_path}")
        except ValueError:
            lines.append("Full path:  (not set)")
        return "\n".join(lines)

    def __str__(self):
        return self.__repr__()

    def setup(self, base_path=None, sample=None, base_name=None):
        """Configure experiment paths.

        Parameters
        ----------
        base_path : str or Path, optional
            Base directory for all experiment data.  Prompted if not given.
        sample : str, optional
            Sample name (used as a sub-folder).  Prompted if not given.
        base_name : str, optional
            Base name for output files.  Prompted if not given.
        """
        if base_path is None:
            base_path = input("Enter base experiment path: ").strip()
        self.base_experiment_path = Path(base_path)

        guess = self.sample or "DefaultSample"
        self.sample = (
            sample or input(f"Enter sample name [{guess}]: ").strip() or guess
        )

        guess = self.file_base_name or "scan"
        self.file_base_name = (
            base_name
            or input(f"Enter file base name [{guess}]: ").strip()
            or guess
        )

        if not self.experiment_path.is_dir():
            self.experiment_path.mkdir(parents=True)

        chdir(self.base_experiment_path)
        print(self.__repr__())

    def change_sample(self, sample=None, base_name=None):
        """Change the sample name and optionally the file base name.

        Parameters
        ----------
        sample : str, optional
            New sample name.  Prompted if not given.
        base_name : str, optional
            New file base name.  Unchanged if not given.
        """
        guess = self.sample or "DefaultSample"
        self.sample = (
            sample or input(f"Enter sample name [{guess}]: ").strip() or guess
        )
        if base_name is not None:
            self.file_base_name = base_name

        if not self.experiment_path.is_dir():
            self.experiment_path.mkdir(parents=True)

        chdir(self.base_experiment_path)
        print(self.__repr__())

    def __call__(self, base_path=None, sample=None, base_name=None):
        self.setup(base_path, sample, base_name)


experiment = ExperimentClass()


def experiment_setup(base_path=None, sample=None, base_name=None):
    """Set up the experiment paths (calls ``experiment.setup()``)."""
    experiment.setup(base_path, sample, base_name)


def experiment_change_sample(sample=None, base_name=None):
    """Change the sample name (calls ``experiment.change_sample()``)."""
    experiment.change_sample(sample, base_name)

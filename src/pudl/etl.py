"""
Run the PUDL ETL Pipeline.

The PUDL project integrates several different public datasets into a well
normalized relational database allowing easier access and interaction between all
datasets. This module coordinates the extract/transfrom/load process for
data from:

 - US Energy Information Agency (EIA):
   - Form 860 (eia860)
   - Form 923 (eia923)
 - US Federal Energy Regulatory Commission (FERC):
   - Form 1 (ferc1)
 - US Environmental Protection Agency (EPA):
   - Continuous Emissions Monitory System (epacems)

"""
import argparse
import logging
import os
import shutil
from pathlib import Path
from typing import Dict

import prefect
import sqlalchemy as sa
from prefect.executors import DaskExecutor
from prefect.executors.dask import LocalDaskExecutor
from prefect.executors.local import LocalExecutor

import pudl
from pudl import dfc
from pudl.fsspec_result import FSSpecResult
from pudl.metadata import RESOURCE_METADATA
from pudl.settings import EtlSettings
from pudl.workflow.dataset_pipeline import DatasetPipeline
from pudl.workflow.epacems import EpaCemsPipeline

logger = logging.getLogger(__name__)

PUDL_META = pudl.metadata.classes.Package.from_resource_ids(RESOURCE_METADATA)


def command_line_flags() -> argparse.ArgumentParser:
    """Returns argparse.ArgumentParser containing flags relevant to the ETL component."""
    parser = argparse.ArgumentParser(
        description="ETL configuration flags", add_help=False)
    # TODO(bendnorman): combine the executors into one arg?
    parser.add_argument(
        "--use-local-dask-executor",
        action="store_true",
        default=False,
        help='If enabled, use LocalDaskExecutor to run the flow.')
    parser.add_argument(
        "--use-dask-executor",
        action="store_true",
        default=False,
        help='If enabled, use local DaskExecutor to run the flow.')
    parser.add_argument(
        "--dask-executor-address",
        default=None,
        help='If specified, use pre-existing DaskExecutor at this address.')
    # TODO(bendnorman): Should this be supported right now?
    parser.add_argument(
        "--upload-to",
        type=str,
        default=os.environ.get('PUDL_UPLOAD_TO'),
        help="""A location (local or remote) where the results of the ETL run
        should be uploaded to. This path will be interpreted by fsspec so
        anything supported by that module is a valid destination.
        This should work with GCS and S3 remote destinations.
        Default value for this will be loaded from PUDL_UPLOAD_TO environment
        variable.
        Files will be stored under {upload_to}/{run_id} to avoid conflicts.
        """)
    # TODO(bendnorman): add this back to the ferc1 datapipline.
    # TODO(bendnorman): should this be T|F? First doesn't make much sense given we don't support multiple datapackages anymore.
    parser.add_argument(
        "--overwrite-ferc1-db",
        action="store_true",
        default=True,
        help="Control whether to rerun the ferc1 database.")
    parser.add_argument(
        "--show-flow-graph",
        action="store_true",
        default=False,
        help="Controls whether flow dependency graphs should be displayed.")
    parser.add_argument(
        "--zenodo-cache-path",
        type=str,
        default=os.environ.get('PUDL_ZENODO_CACHE_PATH'),
        help="""Specifies fsspec-like path where zenodo datapackages are cached.
        This can be local as well as remote location (e.g. GCS or S3).
        If specified, datastore will use this as a read-only cache and will retrieve
        files from this location before contacting Zenodo.
        This is set to read-only mode and will not be modified during ETL run. If you
        need to update or set it up, you can use pudl_datastore CLI to do so.
        Default value for this flag is loaded from PUDL_ZENODO_CACHE_PATH environment
        variable.""")
    # TODO(rousik): the above should be marked as "datastore" cache.
    parser.add_argument(
        "--bypass-local-cache",
        action="store_true",
        default=False,
        help="If enabled, the local file cache for datastore will not be used.")
    # TODO(rousik): the above should also be marked as "datastore" cache
    parser.add_argument(
        "--pipeline-cache-path",
        type=str,
        default=os.environ.get('PUDL_PIPELINE_CACHE_PATH'),
        help="""Controls where the pipeline should be storing its cache. This should be
        used for both the prefect task results as well as for the DataFrameCollections.""")
    parser.add_argument(
        "--keep-cache",
        action="store_true",
        help="""Do not remove local pipeline cache even if the pipeline succeeds. This can
        be used for development/debugging purposes.
        """)
    parser.add_argument(
        "--gcs-requester-pays",
        type=str,
        help="If specified, use this project name to charge the GCS operations to.")

    return parser


###############################################################################
# Coordinating functions
###############################################################################

def log_task_failures(flow_state: prefect.engine.state.State) -> None:
    """Log messages for directly failed tasks."""
    if not flow_state.is_failed():
        return
    for task_instance, task_state in flow_state.result.items():
        if not isinstance(task_state, prefect.engine.state.Failed):
            continue
        if isinstance(task_state, prefect.engine.state.TriggerFailed):
            continue
        logger.error(f'ETL task {task_instance.name} failed: {task_state.message}')


def cleanup_pipeline_cache(state, commandline_args):
    """Runs the pipeline cache cleanup logic, possibly removing the local cache.

    Currently, the cache is destroyed if caching is enabled, if it is done
    locally (not on GCS) and if the flow completed succesfully.
    """
    # TODO(rousik): add --keep-cache=ALWAYS|NEVER|ONFAIL commandline flag to control this
    # TODO(bendnorman): When is this cache actually used? rerun_id?
    if commandline_args.keep_cache:
        logger.warning('--keep-cache prevents cleanup of local cache.')
        return
    if state.is_successful() and commandline_args.pipeline_cache_path:
        cache_root = commandline_args.pipeline_cache_path
        if not cache_root.startswith("gs://"):
            logger.warning(f'Deleting pipeline cache directory under {cache_root}')
            # TODO(rousik): in order to prevent catastrophic results due to misconfiguration
            # we should refuse to delete cache_root unless the directory has the expected
            # run_id form of YYYY-MM-DD-HHMM-uuid4
            shutil.rmtree(cache_root)


def configure_prefect_context(etl_settings, pudl_settings, commandline_args):
    """Sets all pudl ETL relevant variables within prefect context.

    The variables that are set and their meaning:
      * etl_settings: the settings.yml file that configures the operation of the
        pipeline.
    """
    prefect.context.etl_settings = etl_settings
    prefect.context.pudl_settings = pudl_settings
    prefect.context.pudl_upload_to = commandline_args.upload_to
    pudl.workspace.datastore.Datastore.configure_prefect_context(commandline_args)

    pipeline_cache_path = commandline_args.pipeline_cache_path
    if not pipeline_cache_path:
        pipeline_cache_path = os.path.join(pudl_settings["pudl_out"], "cache")
    prefect.context.pudl_pipeline_cache_path = pipeline_cache_path
    prefect.context.data_frame_storage_path = os.path.join(
        pipeline_cache_path, "dataframes")


def etl(  # noqa: C901
    etl_settings: EtlSettings,
    pudl_settings: Dict,
    commandline_args: argparse.Namespace = None
):
    """
    Run the PUDL Extract, Transform, and Load data pipeline.

    First we validate the settings, and then process data destined for loading
    into SQLite, which includes The FERC Form 1 and the EIA Forms 860 and 923.
    Once those data have been output to SQLite we mvoe on to processing the
    long tables, which will be loaded into Apache Parquet files. Some of this
    processing depends on data that's already been loaded into the SQLite DB.

    Args:
        etl_settings: settings that describe datasets to be loaded.
        pudl_settings: a dictionary filled with settings that mostly
            describe paths to various resources and outputs.

    Returns:
        None

    """
    pudl_db_path = Path(pudl_settings["sqlite_dir"]) / "pudl.sqlite"
    if pudl_db_path.exists() and not commandline_args.clobber:
        raise SystemExit(
            "The PUDL DB already exists, and we don't want to clobber it.\n"
            f"Move {pudl_db_path} aside or set clobber=True and try again."
        )

    # TODO (bendnorman): The naming convention here is getting a little wacky.
    validated_etl_settings = etl_settings.datasets

    # Check for existing EPA CEMS outputs if we're going to process CEMS, and
    # do it before running the SQLite part of the ETL so we don't do a bunch of
    # work only to discover that we can't finish.
    datasets = validated_etl_settings.get_datasets()
    if validated_etl_settings.epacems:
        epacems_pq_path = Path(pudl_settings["parquet_dir"]) / "epacems"
        _ = pudl.helpers.prep_dir(epacems_pq_path, clobber=commandline_args.clobber)

    # Setup pipeline cache
    # TODO(bendnorman): what do we want to live in here? How does it affect testing?
    configure_prefect_context(etl_settings, pudl_settings, commandline_args)

    # TODO(bendnorman): How does this differ from pudl_pipeline_cache_path?
    result_cache = os.path.join(prefect.context.pudl_pipeline_cache_path, "prefect")
    flow = prefect.Flow("PUDL ETL", result=FSSpecResult(root_dir=result_cache))

    logger.warning(
        f'Running etl with the following configurations: {sorted(datasets.keys())}')

    # TODO(bendnorman): do I need this?
    prefect.context.dataset_names = datasets.keys()

    # TODO(rousik): we need to have a good way of configuring the datastore caching options
    # from commandline arguments here. Perhaps passing cmdline args by Datastore constructor
    # may do the trick?

    pipelines = {}

    for dataset, settings in datasets.items():
        # Add cems to flow after eia is added.
        if settings and dataset != "epacems":
            pipeline = DatasetPipeline.get_pipeline_for_dataset(dataset)
            pipelines[dataset] = pipeline(flow, settings)

    with flow:
        outputs = []
        for dataset, pl in pipelines.items():
            # TODO(bendnorman) is_excuted() working properly?:
            if pl.is_executed():
                outputs.append(pl.outputs())

        # `tables` is a DataFrame collections containing every dataframe from the pipelines.
        tables = dfc.merge_list(outputs)

        # # Load the ferc1 + eia data directly into the SQLite DB:
        pudl_engine = sa.create_engine(pudl_settings["pudl_db"])
        pudl.load.sqlite.dfs_to_sqlite(
            tables,
            engine=pudl_engine,
            check_foreign_keys=not commandline_args.ignore_foreign_key_constraints,
            check_types=not commandline_args.ignore_type_constraints,
            check_values=not commandline_args.ignore_value_constraints,
        )

    # Add CEMS pipeline to the flow
    if validated_etl_settings.epacems:
        _ = EpaCemsPipeline(
            flow,
            validated_etl_settings.epacems)

    if commandline_args.show_flow_graph:
        flow.visualize()

    # Set the prefect executor.
    prefect_executor = LocalExecutor()
    if commandline_args.use_local_dask_executor:
        prefect_executor = LocalDaskExecutor()
    elif commandline_args.dask_executor_address or commandline_args.use_dask_executor:
        prefect_executor = DaskExecutor(address=commandline_args.dask_executor_address)
    logger.info(f"Using {type(prefect_executor)} Prefect executor.")
    flow.executor = prefect_executor

    state = flow.run()

    log_task_failures(state)
    cleanup_pipeline_cache(state, commandline_args)

    # TODO(rousik): summarize flow errors (directly failed tasks and their execeptions)
    if commandline_args.show_flow_graph:
        flow.visualize(flow_state=state)

    # TODO(rousik): if the flow failed, summarize the failed tasks and throw an exception here.
    # It is unclear whether we want to generate partial results or wipe them.
    return {}

import gc
import json
import logging
import uuid
from collections import deque
from datetime import datetime
from io import StringIO
from typing import Dict, Iterator, List, Optional, Tuple

import azure.durable_functions as df
import azure.functions as func
import pandas as pd
from azure.core.exceptions import ClientAuthenticationError, ResourceNotFoundError
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient
from azure.storage.filedatalake import DataLakeServiceClient
from google.cloud import spanner
from google.oauth2 import service_account

# ============================================================================
# CONSTANTS
# ============================================================================

# Default batch size for processing rows
DEFAULT_BATCH_SIZE = 500

# Limits how many child rows are processed at once to avoid high memory usage.
ARRAY_PROCESSING_BATCH_SIZE = 2000

# File Handling Constants
PIPE_DELIMITER = "|"
ESCAPE_CHARACTER = "\\"

# ============================================================================
# AZURE FUNCTIONS APP
# ============================================================================

app = df.DFApp(http_auth_level=func.AuthLevel.FUNCTION)


# ============================================================================
# KEY VAULT UTILITIES
# ============================================================================


def get_secret(key_vault_name: str, secretname: str) -> str:
    """
    Retrieve a secret from Azure Key Vault.

    Args:
        key_vault_name: Name of the Key Vault
        secretname: Name of the secret to retrieve

    Returns:
        The secret value as a string

    Raises:
        Exception: If secret retrieval fails
    """
    kv_uri = f"https://{key_vault_name}.vault.azure.net"
    credential = DefaultAzureCredential()
    client = SecretClient(vault_url=kv_uri, credential=credential)
    secret = client.get_secret(secretname)
    return secret.value


# ============================================================================
# SPANNER CONNECTION AND METADATA UTILITIES
# ============================================================================


def get_spanner_client(spanner_credentials_json: str, project_id: str):
    """
    Build an authenticated Google Cloud Spanner client.

    Args:
        spanner_credentials_json: JSON string of the service account credentials
        project_id: Google Cloud project ID

    Returns:
        google.cloud.spanner.Client instance
    """
    credentials_info = json.loads(spanner_credentials_json)
    credentials = service_account.Credentials.from_service_account_info(
        credentials_info,
        scopes=["https://www.googleapis.com/auth/spanner.data"],
    )
    return spanner.Client(project=project_id, credentials=credentials)


def validate_spanner_connection(
    spanner_credentials_json: str,
    project_id: str,
    instance_id: str,
    database_id: str,
    table: str,
) -> Tuple:
    """
    Validate Spanner connection and return clients.

    Args:
        spanner_credentials_json: Service account credentials JSON string
        project_id: Google Cloud project ID
        instance_id: Spanner instance ID
        database_id: Spanner database name
        table: Table name to validate

    Returns:
        Tuple of (spanner_client, instance, database)

    Raises:
        ValueError: If validation fails with descriptive error message
    """
    try:
        client = get_spanner_client(spanner_credentials_json, project_id)
        instance = client.instance(instance_id)
        database = instance.database(database_id)

        # Verify table exists by reading one row
        with database.snapshot() as snapshot:
            results = snapshot.execute_sql(
                f"SELECT 1 FROM `{table}` LIMIT 1"
            )
            list(results)  # consume to trigger any errors

        return client, instance, database

    except Exception as e:
        raise ValueError(f"Spanner connection validation failed: {str(e)}")


def get_table_columns(database, table: str) -> List[str]:
    """
    Retrieve column names for a Spanner table from INFORMATION_SCHEMA.

    Args:
        database: Spanner database client
        table: Table name

    Returns:
        Ordered list of column names
    """
    query = (
        "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_NAME = @table ORDER BY ORDINAL_POSITION"
    )
    with database.snapshot() as snapshot:
        results = snapshot.execute_sql(
            query, params={"table": table}, param_types={"table": spanner.param_types.STRING}
        )
        return [row[0] for row in results]


# ============================================================================
# STREAMING SPANNER READER
# ============================================================================


class StreamingSpannerReader:
    """
    Stream rows from a Google Cloud Spanner table in batches.

    Supports both full-table reads and filtered reads based on a
    filter column and set of filter values. Minimizes memory usage
    by yielding one batch at a time.
    """

    def __init__(
        self,
        database,
        table: str,
        columns: List[str],
        filter_column: Optional[str],
        filter_values: Optional[List],
        batch_size: int,
    ):
        """
        Initialize the streaming reader.

        Args:
            database: Spanner database client
            table: Table name to read from
            columns: Ordered list of column names to select
            filter_column: Column name to filter on (None for full scan)
            filter_values: List of values to filter by (None for full scan)
            batch_size: Number of rows per batch
        """
        self.database = database
        self.table = table
        self.columns = columns
        self.filter_column = filter_column
        self.filter_values = filter_values
        self.batch_size = batch_size
        self.operations_count = 0

    def stream_rows(self) -> Iterator[List[Dict]]:
        """
        Stream rows from Spanner in batches.

        Yields:
            Lists of row dicts (batches)
        """
        if self.filter_values:
            for filter_value in self.filter_values:
                yield from self._stream_filtered(filter_value)
        else:
            yield from self._stream_all_rows()

    def _stream_filtered(self, filter_value) -> Iterator[List[Dict]]:
        """
        Stream rows matching a specific filter column value.

        Args:
            filter_value: Value to filter on

        Yields:
            Batches of matching rows as dicts
        """
        query = (
            f"SELECT {', '.join(f'`{c}`' for c in self.columns)} "
            f"FROM `{self.table}` "
            f"WHERE `{self.filter_column}` = @filter_value"
        )
        try:
            with self.database.snapshot() as snapshot:
                results = snapshot.execute_sql(
                    query,
                    params={"filter_value": filter_value},
                    param_types={"filter_value": spanner.param_types.STRING},
                )
                yield from self._process_results(results)
        except Exception as e:
            logging.error(f"Failed to query filter value {filter_value}: {str(e)}")

    def _stream_all_rows(self) -> Iterator[List[Dict]]:
        """
        Stream all rows from the table using a full scan.

        Yields:
            Batches of all rows as dicts
        """
        query = (
            f"SELECT {', '.join(f'`{c}`' for c in self.columns)} "
            f"FROM `{self.table}`"
        )
        try:
            with self.database.snapshot() as snapshot:
                results = snapshot.execute_sql(query)
                yield from self._process_results(results)
        except Exception as e:
            logging.error(f"Failed to query all rows: {str(e)}")

    def _process_results(self, results) -> Iterator[List[Dict]]:
        """
        Convert Spanner result rows into batches of dicts.

        Args:
            results: Spanner StreamedResultSet

        Yields:
            Batches of row dicts
        """
        batch = []
        for row in results:
            row_dict = {}
            for col, val in zip(self.columns, row):
                # Convert non-serialisable Spanner types to strings
                if hasattr(val, "isoformat"):
                    row_dict[col] = val.isoformat()
                elif hasattr(val, "__str__") and not isinstance(val, (int, float, bool, str, type(None))):
                    row_dict[col] = str(val)
                else:
                    row_dict[col] = val

            batch.append(row_dict)
            self.operations_count += 1

            if len(batch) >= self.batch_size:
                yield batch
                batch = []
                gc.collect()

        if batch:
            yield batch
            gc.collect()

    def get_stats(self) -> Dict:
        """
        Get reader statistics.

        Returns:
            Statistics dictionary
        """
        return {"operations_count": self.operations_count}


# ============================================================================
# DATA TRANSFORMATION UTILITIES
# ============================================================================


def clean_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """
    Clean a DataFrame by removing unparseable values.

    Handles:
    - Nested dictionaries and complex objects -> None
    - Arrays of dictionaries -> None
    - Simple values (str, int, float, bool) -> preserved
    - Simple arrays -> preserved

    Args:
        df: DataFrame to clean

    Returns:
        Cleaned DataFrame
    """
    if df is None or df.empty:
        return pd.DataFrame()

    def fix_value(val):
        """Clean individual cell values."""
        if val is None:
            return None
        if isinstance(val, (str, int, float, bool)):
            return val
        if isinstance(val, list) and all(not isinstance(i, dict) for i in val):
            return val
        if isinstance(val, dict):
            return None
        if isinstance(val, list) and any(isinstance(i, dict) for i in val):
            return None
        try:
            return str(val)
        except Exception:
            return None

    for col in df.columns:
        df[col] = df[col].apply(fix_value)

    return df


# ============================================================================
# ADLS FILE UPLOAD
# ============================================================================


def upload_csv_to_adls(
    service_client,
    file_system: str,
    directory: str,
    file_name: str,
    df: pd.DataFrame,
    mode: str = "write",
    known_columns: Optional[set] = None,
) -> set:
    """
    Upload a DataFrame to Azure Data Lake Storage as a CSV file.

    Features:
    - Supports write and append modes
    - Schema evolution: automatically expands schema when new columns appear
    - Pipe-delimited format for better handling of embedded commas
    - UTF-8 encoding for Unicode support

    Args:
        service_client: ADLS service client
        file_system: ADLS container name
        directory: Target directory path
        file_name: Name of the CSV file
        df: DataFrame to upload
        mode: 'write' (overwrite) or 'append'
        known_columns: Set of previously seen column names (for schema tracking)

    Returns:
        Updated set of all known columns
    """
    df = clean_dataframe(df)
    if df is None or df.empty:
        return known_columns or set()

    fs_client = service_client.get_file_system_client(file_system)

    # Create directory hierarchy
    dir_segments = directory.strip("/").split("/") if directory else []
    curr = ""
    for seg in dir_segments:
        curr = f"{curr}/{seg}" if curr else seg
        try:
            fs_client.get_directory_client(curr).create_directory()
        except Exception:
            pass

    dir_client = fs_client.get_directory_client(directory)
    file_client = dir_client.get_file_client(file_name)

    current_columns = set(df.columns)

    if known_columns is None:
        known_columns = current_columns

    new_columns = current_columns - known_columns

    # Handle schema evolution in append mode
    if mode == "append" and new_columns:
        try:
            properties = file_client.get_file_properties()
            if properties.size > 0:
                download = file_client.download_file()
                existing_content = download.readall().decode("utf-8")
                existing_df = pd.read_csv(
                    StringIO(existing_content),
                    sep=PIPE_DELIMITER,
                    keep_default_na=False,
                )

                for col in new_columns:
                    existing_df[col] = None

                known_columns = known_columns | new_columns

                for col in known_columns:
                    if col not in df.columns:
                        df[col] = None

                column_order = sorted(known_columns)
                existing_df = existing_df[column_order]
                df = df[column_order]

                combined_df = pd.concat([existing_df, df], ignore_index=True)

                csv_buffer = StringIO()
                combined_df.to_csv(
                    csv_buffer,
                    index=False,
                    sep=PIPE_DELIMITER,
                    header=True,
                    quoting=0,
                    escapechar=ESCAPE_CHARACTER,
                    na_rep="",
                )
                content = csv_buffer.getvalue()

                file_client.delete_file()
                file_client = dir_client.create_file(file_name)
                content_bytes = content.encode("utf-8")
                file_client.append_data(content_bytes, offset=0, length=len(content_bytes))
                file_client.flush_data(len(content_bytes))

                return known_columns

        except Exception:
            pass

    # Standard write/append
    for col in known_columns:
        if col not in df.columns:
            df[col] = None

    if len(known_columns) > 0:
        df = df[sorted(known_columns)]

    try:
        if mode == "write":
            try:
                file_client.delete_file()
            except Exception:
                logging.debug(f"Could not delete {file_name} before write — may not exist yet")

            csv_buffer = StringIO()
            df.to_csv(
                csv_buffer,
                index=False,
                sep=PIPE_DELIMITER,
                header=True,
                quoting=0,
                escapechar=ESCAPE_CHARACTER,
                na_rep="",
            )
            content = csv_buffer.getvalue()

            file_client = dir_client.create_file(file_name)
            content_bytes = content.encode("utf-8")
            file_client.append_data(content_bytes, offset=0, length=len(content_bytes))
            file_client.flush_data(len(content_bytes))
        else:
            # Append mode
            file_exists = False
            current_size = 0

            try:
                properties = file_client.get_file_properties()
                current_size = properties.size
                file_exists = True
            except Exception:
                file_exists = False

            csv_buffer = StringIO()
            include_header = not file_exists
            df.to_csv(
                csv_buffer,
                index=False,
                sep=PIPE_DELIMITER,
                header=include_header,
                quoting=0,
                escapechar=ESCAPE_CHARACTER,
                na_rep="",
            )
            content = csv_buffer.getvalue()

            if not file_exists:
                file_client = dir_client.create_file(file_name)
                current_size = 0

            content_bytes = content.encode("utf-8")
            file_client.append_data(content_bytes, offset=current_size, length=len(content_bytes))
            file_client.flush_data(current_size + len(content_bytes))

    except Exception as e:
        logging.error(f"Upload failed for {file_name}: {str(e)}")
        raise

    known_columns = known_columns | current_columns
    return known_columns


def validate_adls_connection(adls_account_name: str, adls_file_system: str):
    """
    Validate ADLS connection and return service client.

    Args:
        adls_account_name: Storage account name
        adls_file_system: Container/filesystem name

    Returns:
        DataLakeServiceClient instance

    Raises:
        ValueError: If validation fails with descriptive error message
    """
    try:
        credential = DefaultAzureCredential()
        service_client = DataLakeServiceClient(
            account_url=f"https://{adls_account_name}.dfs.core.windows.net",
            credential=credential,
        )

        fs_client = service_client.get_file_system_client(adls_file_system)
        list(fs_client.get_paths(path="", max_results=1))

        return service_client

    except ClientAuthenticationError:
        raise ValueError(
            "ADLS authentication failed. Ensure managed identity has proper permissions."
        )
    except ResourceNotFoundError:
        raise ValueError(f"ADLS filesystem '{adls_file_system}' does not exist.")
    except Exception as e:
        raise ValueError(f"ADLS connection validation failed: {str(e)}")


# ============================================================================
# PARAMETER VALIDATION AND EXTRACTION
# ============================================================================


def validate_and_extract_params(params: dict) -> dict:
    """
    Validate and extract parameters from the input dictionary.

    Args:
        params: Input parameters dictionary

    Returns:
        Validated and processed parameters

    Raises:
        ValueError: If required parameters are missing or invalid
    """
    spanner_endpoint = params.get("spanner_endpoint")
    key_vault = params.get("key_vault_name")
    secret_name = params.get("spanner_credentials_secret_name")
    spanner_database_id = params.get("spanner_database_id")
    spanner_table = params.get("spanner_table")
    adls_account_name = params.get("adls_account_name")
    adls_file_system = params.get("adls_file_system")

    missing = [
        k
        for k, v in {
            "Spanner endpoint (project/instance)": spanner_endpoint,
            "Spanner credentials secret name": secret_name,
            "Spanner database ID": spanner_database_id,
            "Key Vault name": key_vault,
            "Spanner table name": spanner_table,
            "Storage account name": adls_account_name,
            "ADLS container name": adls_file_system,
        }.items()
        if not v
    ]

    if missing:
        raise ValueError(f"Missing required parameters: {missing}")

    # spanner_endpoint format: "<project_id>/<instance_id>"
    try:
        project_id, instance_id = spanner_endpoint.strip("/").split("/", 1)
    except ValueError:
        raise ValueError(
            "spanner_endpoint must be in the format '<project_id>/<instance_id>'"
        )

    filter_column = params.get("filter_column")
    filter_values = params.get("filter_value")
    adls_directory = params.get("adls_directory", "")
    adls_file_prefix = params.get("adls_file_prefix", "")
    separate_files_per_batch = params.get("separate_files_per_batch", False)

    batch_size = params.get("batch_size", DEFAULT_BATCH_SIZE)
    try:
        batch_size = int(batch_size)
        if batch_size < 1:
            batch_size = DEFAULT_BATCH_SIZE
    except (ValueError, TypeError):
        batch_size = DEFAULT_BATCH_SIZE

    return {
        "project_id": project_id,
        "instance_id": instance_id,
        "spanner_database_id": spanner_database_id,
        "spanner_table": spanner_table,
        "key_vault": key_vault,
        "secret_name": secret_name,
        "filter_column": filter_column,
        "filter_values": filter_values,
        "adls_account_name": adls_account_name,
        "adls_file_system": adls_file_system,
        "adls_directory": adls_directory,
        "adls_file_prefix": adls_file_prefix,
        "batch_size": batch_size,
        "separate_files_per_batch": separate_files_per_batch,
    }


def setup_filter_configuration(params: dict) -> Tuple[Optional[str], Optional[List], bool]:
    """
    Setup filter column configuration for querying.

    Args:
        params: Validated parameters

    Returns:
        Tuple of (filter_column, filter_values, user_provided_both)

    Raises:
        ValueError: If filter configuration is invalid
    """
    filter_column = params["filter_column"]
    filter_values = params["filter_values"]

    has_filter_column = bool(filter_column)
    has_filter_values = bool(filter_values)

    if has_filter_column != has_filter_values:
        raise ValueError(
            "Invalid filter configuration: "
            "Both 'filter_column' AND 'filter_value' must be provided together."
        )

    user_provided_both = has_filter_column and has_filter_values

    if user_provided_both:
        if isinstance(filter_values, str):
            try:
                resolved_values = json.loads(filter_values)
            except Exception:
                resolved_values = [filter_values]
        elif isinstance(filter_values, list):
            resolved_values = filter_values
        else:
            resolved_values = [filter_values]

        if not resolved_values:
            raise ValueError("filter_value was provided but is empty.")

        return filter_column, resolved_values, user_provided_both
    else:
        return None, None, user_provided_both


def process_row_batch(batch_rows: List[Dict]) -> pd.DataFrame:
    """
    Convert a batch of row dicts into a DataFrame.

    Args:
        batch_rows: List of row dicts from Spanner

    Returns:
        DataFrame of the batch
    """
    return pd.DataFrame(batch_rows)


# ============================================================================
# MAIN ACTIVITY FUNCTION
# ============================================================================


@app.activity_trigger(input_name="params")
def process_spanner_to_adls_activity(params: dict):
    """
    Azure Durable Function activity to export Spanner data to ADLS.

    This function:
    1. Validates connections to Spanner and ADLS
    2. Discovers table columns from INFORMATION_SCHEMA
    3. Optionally filters rows by a specified column and value list
    4. Streams rows in batches to minimize memory usage
    5. Uploads data to ADLS as pipe-delimited CSV files

    Args:
        params: Dictionary containing configuration parameters

    Returns:
        Dictionary with execution results and statistics
    """
    start_time = datetime.utcnow()

    try:
        params = validate_and_extract_params(params)

        spanner_credentials_json = get_secret(
            key_vault_name=params["key_vault"], secretname=params["secret_name"]
        )

        _, _, database = validate_spanner_connection(
            spanner_credentials_json=spanner_credentials_json,
            project_id=params["project_id"],
            instance_id=params["instance_id"],
            database_id=params["spanner_database_id"],
            table=params["spanner_table"],
        )

        service_client = validate_adls_connection(
            params["adls_account_name"], params["adls_file_system"]
        )

        columns = get_table_columns(database, params["spanner_table"])
        if not columns:
            raise ValueError(
                f"No columns found for table '{params['spanner_table']}'. "
                "Verify the table exists and the service account has access."
            )

        filter_column, filter_values, user_provided_both = setup_filter_configuration(params)

        streaming_reader = StreamingSpannerReader(
            database=database,
            table=params["spanner_table"],
            columns=columns,
            filter_column=filter_column,
            filter_values=filter_values,
            batch_size=params["batch_size"],
        )

        export_dir = (
            f"{params['adls_directory']}/{params['spanner_table']}"
            if params["adls_directory"]
            else params["spanner_table"]
        )

        rows_processed = 0
        batch_num = 0
        parent_schema = None

        for batch_rows in streaming_reader.stream_rows():
            batch_num += 1

            parent_df = process_row_batch(batch_rows)

            if params["separate_files_per_batch"]:
                file_name = f"{params['spanner_table']}_batch_{batch_num:05d}.csv"
                mode = "write"
                parent_schema = None
            else:
                file_name = f"{params['spanner_table']}.csv"
                mode = "append" if batch_num > 1 else "write"

            parent_schema = upload_csv_to_adls(
                service_client,
                params["adls_file_system"],
                export_dir,
                file_name,
                parent_df,
                mode,
                parent_schema,
            )

            rows_processed += len(batch_rows)

            del parent_df
            del batch_rows
            gc.collect()

        if rows_processed == 0:
            return {
                "status": "success",
                "message": "No rows found.",
                "rows_processed": 0,
            }

        streaming_stats = streaming_reader.get_stats()
        duration = (datetime.utcnow() - start_time).total_seconds()

        return {
            "status": "success",
            "rows_processed": rows_processed,
            "batch_size": params["batch_size"],
            "batches_processed": batch_num,
            "separate_files_per_batch": params["separate_files_per_batch"],
            "duration_seconds": round(duration, 2),
            "filter_applied": user_provided_both,
            "filter_value_count": len(filter_values) if filter_values else 0,
            "operations_count": streaming_stats["operations_count"],
        }

    except Exception as e:
        logging.error(f"Activity failed: {str(e)}", exc_info=True)
        return {"status": "error", "message": str(e)}


# ============================================================================
# ORCHESTRATOR AND HTTP TRIGGER
# ============================================================================


@app.orchestration_trigger(context_name="context")
def spanner_to_adls_orchestrator(context: df.DurableOrchestrationContext):
    """
    Durable orchestrator for the Spanner to ADLS export process.

    Args:
        context: Durable orchestration context

    Returns:
        Result from the activity function
    """
    params = context.get_input()
    result = yield context.call_activity("process_spanner_to_adls_activity", params)
    return result


@app.route(route="Spanner_to_ADLS_V1", methods=["POST"])
@app.durable_client_input(client_name="client")
async def spanner_to_adls_http_start(req: func.HttpRequest, client) -> func.HttpResponse:
    """
    HTTP trigger to start the Spanner to ADLS export orchestration.

    Args:
        req: HTTP request with JSON body containing export parameters
        client: Durable orchestration client

    Returns:
        HTTP response with orchestration status URLs
    """
    try:
        body = req.get_json()
    except Exception:
        return func.HttpResponse("Invalid JSON body", status_code=400)

    try:
        instance_id = await client.start_new("spanner_to_adls_orchestrator", None, body)
        response = client.create_check_status_response(req, instance_id)
        return response

    except Exception as e:
        logging.error(f"HTTP start failed: {str(e)}", exc_info=True)
        return func.HttpResponse(
            json.dumps({"error": str(e)}), mimetype="application/json", status_code=500
        )

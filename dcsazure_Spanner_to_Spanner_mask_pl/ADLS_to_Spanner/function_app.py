import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Tuple

import azure.durable_functions as df
import azure.functions as func
import pandas as pd
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient
from azure.storage.filedatalake import DataLakeServiceClient
from google.cloud import spanner
from google.oauth2 import service_account

app = df.DFApp(http_auth_level=func.AuthLevel.FUNCTION)

# Concurrency limits
MIN_CONCURRENT_OPERATIONS = 5
MAX_CONCURRENT_OPERATIONS = 200

# Retry configuration
RETRY_MAX_ATTEMPTS = 10
RETRY_BASE_DELAY = 1
BACKOFF_MULTIPLIER = 2

# Logging interval
PROGRESS_LOG_INTERVAL = 100

# File Handling Constants
PIPE_DELIMITER = "|"
ESCAPE_CHARACTER = "\\"


def get_secret(key_vault_name: str, secretname: str) -> str:
    """
    Retrieve a secret from Azure Key Vault.

    Uses DefaultAzureCredential for authentication and connects to the specified
    Key Vault to retrieve the named secret.

    Args:
        key_vault_name: Name of the Key Vault (without .vault.azure.net suffix)
        secretname: Name of the secret to retrieve

    Returns:
        The secret value as a string

    Raises:
        Exception: If secret retrieval fails due to authentication issues,
                  missing secret, or network problems
    """
    try:
        kv_uri = f"https://{key_vault_name}.vault.azure.net"
        credential = DefaultAzureCredential()
        client = SecretClient(vault_url=kv_uri, credential=credential)
        secret = client.get_secret(secretname)
        return secret.value
    except Exception as e:
        logging.error(
            f"Failed to retrieve secret '{secretname}' from Key Vault '{key_vault_name}': {str(e)}"
        )
        raise


def get_spanner_client(service_account_json: str, project_id: str) -> spanner.Client:
    """
    Create an authenticated Google Cloud Spanner client.

    Parses the service account JSON key retrieved from Key Vault and constructs
    an authenticated Spanner client scoped to the given project.

    Args:
        service_account_json: JSON string containing the Google service account key
        project_id: Google Cloud project ID

    Returns:
        Authenticated google.cloud.spanner.Client instance

    Raises:
        Exception: If the service account JSON is invalid or authentication fails
    """
    try:
        credentials_info = json.loads(service_account_json)
        credentials = service_account.Credentials.from_service_account_info(
            credentials_info,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        return spanner.Client(project=project_id, credentials=credentials)
    except Exception as e:
        logging.error(f"Failed to create Spanner client: {str(e)}")
        raise


def get_csv_row_count(
    service_client: DataLakeServiceClient,
    file_system: str,
    full_path: str,
    chunk_size: int = 50000,
) -> int:
    """
    Get total row count from CSV by streaming with proper CSV parsing.

    Streams the CSV file in chunks to avoid loading the entire file into memory,
    which is important for large files.

    Args:
        service_client: ADLS service client for connecting to storage
        file_system: ADLS file system (container) name
        full_path: Full path to CSV file within the file system
        chunk_size: Number of rows to process per chunk (default: 50000)

    Returns:
        Total number of rows in the CSV file (excluding header)

    Raises:
        Exception: If reading the CSV fails due to access issues, invalid format,
                  or network problems
    """
    try:
        fs_client = service_client.get_file_system_client(file_system=file_system)
        file_client = fs_client.get_file_client(full_path)
        download = file_client.download_file()

        total_rows = 0
        chunk_iterator = pd.read_csv(
            download,
            sep=PIPE_DELIMITER,
            chunksize=chunk_size,
            iterator=True,
            quoting=0,
            keep_default_na=False,
            escapechar=ESCAPE_CHARACTER,
        )

        for chunk in chunk_iterator:
            total_rows += len(chunk)
            del chunk

        return total_rows

    except Exception as e:
        logging.error(f"Error counting rows in CSV '{full_path}': {str(e)}")
        raise


def read_csv_from_adls_batched(
    service_client: DataLakeServiceClient,
    file_system: str,
    full_path: str,
    skip_rows: int = 0,
    nrows: int = None,
) -> pd.DataFrame:
    """
    Read CSV from ADLS in batches using streaming.

    Efficiently reads a portion of a CSV file from ADLS by skipping rows and
    limiting the number of rows read. Uses pipe (|) as delimiter.

    Args:
        service_client: ADLS service client for connecting to storage
        file_system: ADLS file system (container) name
        full_path: Full path to CSV file within the file system
        skip_rows: Number of rows to skip after header (default: 0)
        nrows: Maximum number of rows to read (default: None for all rows)

    Returns:
        DataFrame containing the requested rows

    Raises:
        Exception: If reading the CSV fails due to access issues, invalid format,
                  or network problems
    """
    try:
        fs_client = service_client.get_file_system_client(file_system=file_system)
        file_client = fs_client.get_file_client(full_path)
        download = file_client.download_file()

        read_params = {
            "sep": PIPE_DELIMITER,
            "iterator": False,
            "quoting": 0,
            "keep_default_na": False,
            "escapechar": ESCAPE_CHARACTER,
        }

        if skip_rows > 0:
            read_params["skiprows"] = range(1, skip_rows + 1)
        if nrows:
            read_params["nrows"] = nrows

        df = pd.read_csv(download, **read_params)
        return df

    except Exception as e:
        logging.error(
            f"Error reading CSV '{full_path}' (skip_rows={skip_rows}, nrows={nrows}): {str(e)}"
        )
        raise


def get_spanner_table_columns(
    instance_id: str,
    database_id: str,
    table_name: str,
    spanner_client: spanner.Client,
) -> List[str]:
    """
    Retrieve column names for a Spanner table from the information schema.

    Queries the INFORMATION_SCHEMA.COLUMNS view to obtain the ordered list of
    column names for the target table. Column order matches the table's primary
    key and definition order, which is required for correct mutation construction.

    Args:
        instance_id: Spanner instance ID
        database_id: Spanner database ID
        table_name: Name of the table to introspect
        spanner_client: Authenticated Spanner client

    Returns:
        List of column name strings in ordinal position order

    Raises:
        Exception: If the table does not exist or the query fails
    """
    try:
        instance = spanner_client.instance(instance_id)
        database = instance.database(database_id)

        with database.snapshot() as snapshot:
            results = snapshot.execute_sql(
                """
                SELECT COLUMN_NAME
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_NAME = @table_name
                ORDER BY ORDINAL_POSITION
                """,
                params={"table_name": table_name},
                param_types={"table_name": spanner.param_types.STRING},
            )
            columns = [row[0] for row in results]

        if not columns:
            raise ValueError(
                f"Table '{table_name}' not found in database '{database_id}' "
                f"or has no columns."
            )

        logging.info(
            f"Discovered {len(columns)} columns for table '{table_name}': {columns}"
        )
        return columns

    except Exception as e:
        logging.error(
            f"Failed to retrieve columns for table '{table_name}': {str(e)}"
        )
        raise


def coerce_row_types(
    row: Dict[str, Any], columns: List[str]
) -> List[Any]:
    """
    Coerce a row dictionary into an ordered list of values with Spanner-safe types.

    Iterates the ordered column list and extracts values from the row dictionary,
    converting pandas NA / None to Python None so the Spanner client does not
    receive NaN or NaT values, which it cannot serialise.

    Args:
        row: Dictionary mapping column names to raw values (from CSV)
        columns: Ordered list of column names matching the Spanner table schema

    Returns:
        Ordered list of values suitable for use in a Spanner Mutation
    """
    values = []
    for col in columns:
        val = row.get(col)
        if val is None or (isinstance(val, float) and pd.isna(val)):
            values.append(None)
        elif isinstance(val, pd.Timestamp):
            values.append(val.isoformat())
        else:
            values.append(val)
    return values


def build_mutations(
    rows: List[Dict[str, Any]],
    table_name: str,
    columns: List[str],
) -> List[spanner.Mutation]:
    """
    Build a list of Spanner insert-or-update Mutations from a list of row dicts.

    Each row is converted to an ordered value list using coerce_row_types, then
    wrapped in an insert_or_update Mutation. Missing columns receive None.

    Args:
        rows: List of row dictionaries (column name -> value)
        table_name: Target Spanner table name
        columns: Ordered list of column names to include in each mutation

    Returns:
        List of google.cloud.spanner.Mutation objects ready for commit
    """
    mutations = []
    for row in rows:
        values = coerce_row_types(row, columns)
        mutations.append(
            spanner.Mutation.insert_or_update(table_name, columns, values)
        )
    return mutations


def write_batch_to_spanner(
    instance_id: str,
    database_id: str,
    table_name: str,
    columns: List[str],
    rows: List[Dict[str, Any]],
    spanner_client: spanner.Client,
    batch_index: int,
) -> Tuple[int, int]:
    """
    Write a single batch of rows to Spanner using a commit with mutations.

    Constructs insert_or_update mutations for every row and commits them in one
    transaction. Retries on ABORTED errors (Spanner transaction contention) using
    exponential backoff up to RETRY_MAX_ATTEMPTS attempts.

    Args:
        instance_id: Spanner instance ID
        database_id: Spanner database ID
        table_name: Target Spanner table name
        columns: Ordered list of column names
        rows: List of row dictionaries to write
        spanner_client: Authenticated Spanner client
        batch_index: Batch sequence number (used for logging)

    Returns:
        Tuple of (successful_rows, failed_rows)

    Raises:
        Exception: If all retry attempts are exhausted
    """
    instance = spanner_client.instance(instance_id)
    database = instance.database(database_id)
    mutations = build_mutations(rows, table_name, columns)

    for attempt in range(RETRY_MAX_ATTEMPTS):
        try:
            database.run_in_transaction(
                lambda transaction: transaction.batch_update(mutations)
                if False
                else _commit_mutations(database, mutations)
            )
            logging.info(
                f"Batch {batch_index}: committed {len(rows)} rows to '{table_name}'"
            )
            return len(rows), 0

        except Exception as e:
            error_str = str(e).lower()
            is_retryable = any(
                keyword in error_str
                for keyword in ("aborted", "deadline", "unavailable", "internal")
            )

            if is_retryable and attempt < RETRY_MAX_ATTEMPTS - 1:
                wait_time = RETRY_BASE_DELAY * (BACKOFF_MULTIPLIER ** attempt)
                logging.warning(
                    f"Batch {batch_index}: retryable error on attempt {attempt + 1}, "
                    f"retrying in {wait_time}s: {str(e)[:100]}"
                )
                time.sleep(wait_time)
                continue

            logging.error(
                f"Batch {batch_index}: failed after {attempt + 1} attempt(s): {str(e)}"
            )
            return 0, len(rows)

    return 0, len(rows)


def _commit_mutations(database, mutations: List[spanner.Mutation]):
    """
    Commit a list of Spanner Mutations using the Batch API.

    Uses the Spanner Batch context manager to commit all supplied mutations in a
    single round-trip. This is preferred over run_in_transaction for bulk writes
    because it avoids the read-write transaction overhead.

    Args:
        database: google.cloud.spanner.Database instance
        mutations: List of Mutation objects to commit

    Raises:
        Exception: Propagates any Spanner API error from the commit call
    """
    with database.batch() as batch:
        for mutation in mutations:
            if mutation.insert_or_update:
                table = mutation.insert_or_update.table
                cols = list(mutation.insert_or_update.columns)
                vals = [list(row.values) for row in mutation.insert_or_update.values]
                for row_vals in vals:
                    batch.insert_or_update(table=table, columns=cols, values=[row_vals])


def truncate_spanner_table(
    instance_id: str,
    database_id: str,
    table_name: str,
    spanner_client: spanner.Client,
):
    """
    Delete all rows from a Spanner table using a DML DELETE statement.

    Issues a partitioned DML DELETE to remove every row from the target table
    before writing masked data. Partitioned DML is used for large-scale deletes
    because it does not require holding a read lock over the entire table.

    Args:
        instance_id: Spanner instance ID
        database_id: Spanner database ID
        table_name: Table to truncate
        spanner_client: Authenticated Spanner client

    Raises:
        Exception: If the DELETE DML fails (e.g., table does not exist, permissions)
    """
    try:
        instance = spanner_client.instance(instance_id)
        database = instance.database(database_id)
        row_count = database.execute_partitioned_dml(
            f"DELETE FROM `{table_name}` WHERE TRUE"
        )
        logging.info(
            f"Truncated table '{table_name}': approximately {row_count} rows deleted"
        )
    except Exception as e:
        logging.error(f"Failed to truncate table '{table_name}': {str(e)}")
        raise


def process_adls_to_spanner(body: Dict) -> Dict:
    """
    Main processing function to transfer masked data from ADLS into Google Cloud Spanner.

    Orchestrates the entire data-load process:
    1. Validates parameters and retrieves secrets from Azure Key Vault
    2. Connects to ADLS and Google Cloud Spanner
    3. Optionally truncates the target table before writing
    4. Discovers the Spanner table schema (column names and order)
    5. Reads the staged masked CSV from ADLS in configurable batches
    6. Writes each batch to Spanner using insert_or_update mutations
    7. Returns comprehensive statistics

    Args:
        body: Request body containing configuration parameters:
            - spanner_project_id: Google Cloud project ID
            - spanner_instance_id: Spanner instance ID
            - spanner_sink_database: Spanner database name for masked data
            - spanner_table: Spanner table name
            - key_vault_name: Azure Key Vault name for secrets
            - spanner_secret_name: Secret name holding the GCP service account JSON key
            - adls_secret_name: Secret name holding the ADLS storage account key
            - adls_account_name: ADLS storage account name
            - adls_file_system: ADLS file system (container) name
            - adls_directory: Directory path within ADLS where masked CSV is staged
            - truncate_sink_before_write: Boolean – delete all rows before loading
            - batch_size: Number of rows to commit per Spanner transaction (default 100000)

    Returns:
        Dictionary with processing results and statistics:
        - status: "completed" or "completed_with_errors"
        - spanner_configuration: Target table and database details
        - data_processing: Row counts and batch information
        - results: Success/failure counts and success rate
        - performance_metrics: Elapsed time and throughput
        - failed_batch_indices: Indices of batches that failed (if any)

    Raises:
        ValueError: If required parameters are missing
        Exception: For connection, CSV parsing, or Spanner write errors
    """
    params = _extract_parameters(body)
    _validate_parameters(params)

    # Retrieve secrets from Azure Key Vault
    spanner_service_account_json = get_secret(
        key_vault_name=params["key_vault"],
        secretname=params["spanner_secret_name"],
    )

    # Build ADLS path to masked CSV
    adls_csv_path = (
        f"{params['adls_directory']}/{params['spanner_table']}/{params['spanner_table']}.csv"
    ).strip("/")

    try:
        # Initialize ADLS client using managed identity
        adls_credential = DefaultAzureCredential()
        service_client = DataLakeServiceClient(
            account_url=f"https://{params['adls_account_name']}.dfs.core.windows.net",
            credential=adls_credential,
        )

        # Count total rows in the staged CSV
        total_rows = get_csv_row_count(
            service_client,
            params["adls_file_system"],
            adls_csv_path,
            chunk_size=params["batch_size"],
        )
        logging.info(
            f"Total rows to load into Spanner table '{params['spanner_table']}': {total_rows}"
        )

        # Initialize Spanner client
        spanner_client = get_spanner_client(
            spanner_service_account_json, params["spanner_project_id"]
        )

        # Optionally truncate target table before writing
        if params["truncate"]:
            truncate_spanner_table(
                instance_id=params["spanner_instance_id"],
                database_id=params["spanner_sink_database"],
                table_name=params["spanner_table"],
                spanner_client=spanner_client,
            )

        # Discover table schema
        columns = get_spanner_table_columns(
            instance_id=params["spanner_instance_id"],
            database_id=params["spanner_sink_database"],
            table_name=params["spanner_table"],
            spanner_client=spanner_client,
        )

        # Process all batches
        result = _process_all_batches(
            service_client=service_client,
            params=params,
            adls_csv_path=adls_csv_path,
            total_rows=total_rows,
            columns=columns,
            spanner_client=spanner_client,
        )

        return _build_response(result, params, total_rows, columns)

    except Exception as e:
        logging.error(f"Processing failed: {str(e)}")
        raise


def _extract_parameters(body: Dict) -> Dict:
    """
    Extract parameters from the HTTP request body.

    Parses the request body and extracts all required and optional configuration
    parameters with appropriate defaults.

    Args:
        body: Request body dictionary from the HTTP trigger

    Returns:
        Dictionary with extracted and normalised parameters
    """
    return {
        "spanner_project_id": body.get("spanner_project_id"),
        "spanner_instance_id": body.get("spanner_instance_id"),
        "spanner_sink_database": body.get("spanner_sink_database"),
        "spanner_table": body.get("spanner_table"),
        "key_vault": body.get("key_vault_name"),
        "spanner_secret_name": body.get("spanner_secret_name"),
        "adls_secret_name": body.get("adls_secret_name"),
        "adls_account_name": body.get("adls_account_name"),
        "adls_file_system": body.get("adls_file_system"),
        "adls_directory": body.get("adls_directory", ""),
        "truncate": body.get("truncate_sink_before_write"),
        "batch_size": body.get("batch_size", 100000),
    }


def _validate_parameters(params: Dict):
    """
    Validate that all required parameters are present.

    Checks the parameters dictionary and raises ValueError if any required keys
    are missing or empty.

    Args:
        params: Parameters dictionary from _extract_parameters()

    Raises:
        ValueError: If any required parameters are missing or if
                    truncate_sink_before_write is not provided
    """
    missing = [
        k
        for k, v in {
            "Spanner Project ID": params["spanner_project_id"],
            "Spanner Instance ID": params["spanner_instance_id"],
            "Spanner Sink Database": params["spanner_sink_database"],
            "Spanner Table": params["spanner_table"],
            "Key Vault name": params["key_vault"],
            "Spanner Secret name": params["spanner_secret_name"],
            "ADLS Secret name": params["adls_secret_name"],
            "Storage account name": params["adls_account_name"],
            "ADLS file system": params["adls_file_system"],
            "ADLS Directory": params["adls_directory"],
        }.items()
        if not v
    ]

    if missing:
        error_msg = f"Missing required parameters: {missing}"
        logging.error(error_msg)
        raise ValueError(error_msg)

    if params["truncate"] is None:
        error_msg = "Missing required parameter: truncate_sink_before_write"
        logging.error(error_msg)
        raise ValueError(error_msg)


def _process_all_batches(
    service_client: DataLakeServiceClient,
    params: Dict,
    adls_csv_path: str,
    total_rows: int,
    columns: List[str],
    spanner_client: spanner.Client,
) -> Dict:
    """
    Iterate over all row batches in the staged CSV and write each to Spanner.

    Reads the ADLS CSV in sequential batches of params['batch_size'] rows,
    converts each batch to a list of row dictionaries, and calls
    write_batch_to_spanner for each. Aggregates success/failure counts and
    timing across all batches.

    Args:
        service_client: ADLS DataLakeServiceClient instance
        params: Validated parameters dictionary
        adls_csv_path: Full ADLS path to the staged masked CSV
        total_rows: Total number of data rows in the CSV (excluding header)
        columns: Ordered list of Spanner column names
        spanner_client: Authenticated Spanner client

    Returns:
        Aggregated result dictionary with keys:
        - total: Total rows attempted
        - successful: Rows successfully written
        - failed: Rows that could not be written
        - num_batches: Number of batches processed
        - elapsed_seconds: Wall-clock time for all batches combined
        - failed_batch_indices: List of batch indices that failed
    """
    total_successful = 0
    total_failed = 0
    total_elapsed = 0.0
    failed_batch_indices = []

    num_batches = (total_rows + params["batch_size"] - 1) // params["batch_size"]

    for batch_num in range(num_batches):
        skip_rows = batch_num * params["batch_size"]
        current_batch_size = min(params["batch_size"], total_rows - skip_rows)

        logging.info(
            f"Processing batch {batch_num + 1}/{num_batches}: "
            f"rows {skip_rows + 1} to {skip_rows + current_batch_size}"
        )

        batch_df = read_csv_from_adls_batched(
            service_client,
            params["adls_file_system"],
            adls_csv_path,
            skip_rows=skip_rows,
            nrows=current_batch_size,
        )

        rows = batch_df.where(pd.notnull(batch_df), None).to_dict(orient="records")
        del batch_df

        start = time.time()
        successful, failed = write_batch_to_spanner(
            instance_id=params["spanner_instance_id"],
            database_id=params["spanner_sink_database"],
            table_name=params["spanner_table"],
            columns=columns,
            rows=rows,
            spanner_client=spanner_client,
            batch_index=batch_num + 1,
        )
        elapsed = time.time() - start

        total_successful += successful
        total_failed += failed
        total_elapsed += elapsed

        if failed > 0:
            failed_batch_indices.append(batch_num + 1)

        del rows

    return {
        "total": total_rows,
        "successful": total_successful,
        "failed": total_failed,
        "num_batches": num_batches,
        "elapsed_seconds": total_elapsed,
        "failed_batch_indices": failed_batch_indices,
    }


def _build_response(
    result: Dict,
    params: Dict,
    total_rows: int,
    columns: List[str],
) -> Dict:
    """
    Build the final response dictionary from processing results.

    Assembles all statistics and configuration details into a structured
    response for the ADF pipeline to inspect.

    Args:
        result: Results dictionary from _process_all_batches()
        params: Validated parameters dictionary
        total_rows: Total rows that were staged for loading
        columns: List of Spanner column names that were written

    Returns:
        Comprehensive response dictionary with sections for configuration,
        data processing counts, success/failure results, performance metrics,
        and any failed batch indices
    """
    elapsed = result["elapsed_seconds"]
    total = result["total"]
    successful = result["successful"]

    return {
        "status": "completed" if result["failed"] == 0 else "completed_with_errors",
        "spanner_configuration": {
            "project_id": params["spanner_project_id"],
            "instance_id": params["spanner_instance_id"],
            "database": params["spanner_sink_database"],
            "table": params["spanner_table"],
            "columns_loaded": columns,
            "truncated_before_write": params["truncate"],
        },
        "data_processing": {
            "total_rows_staged": total_rows,
            "batch_size": params["batch_size"],
            "num_batches_processed": result["num_batches"],
        },
        "results": {
            "total_rows": total,
            "successful": successful,
            "failed": result["failed"],
            "success_rate_percent": (
                round(100 * successful / total, 2) if total > 0 else 0
            ),
        },
        "performance_metrics": {
            "elapsed_seconds": round(elapsed, 2),
            "row_rate_per_second": (
                round(total / elapsed, 2) if elapsed > 0 else 0
            ),
        },
        "failed_batch_indices": result["failed_batch_indices"],
    }


@app.activity_trigger(input_name="params")
def process_adls_to_spanner_activity(params: dict):
    """
    Activity trigger for processing ADLS to Spanner transfer.

    This is the Durable Functions activity that wraps the synchronous processing
    function. It is invoked by the orchestrator and runs process_adls_to_spanner
    with the supplied parameters.

    Args:
        params: Parameters dictionary containing all configuration

    Returns:
        Processing results dictionary from process_adls_to_spanner()
    """
    return process_adls_to_spanner(params)


@app.orchestration_trigger(context_name="context")
def adls_to_spanner_orchestrator(context: df.DurableOrchestrationContext):
    """
    Orchestrator for ADLS to Spanner transfer.

    Receives the input payload from the HTTP trigger and delegates to the
    process_adls_to_spanner_activity activity function.

    Args:
        context: Durable orchestration context

    Returns:
        Activity result dictionary
    """
    params = context.get_input()
    result = yield context.call_activity("process_adls_to_spanner_activity", params)
    return result


@app.route(route="ADLS_to_Spanner_V1", methods=["POST"])
@app.durable_client_input(client_name="client")
async def adls_to_spanner_http_start(
    req: func.HttpRequest, client
) -> func.HttpResponse:
    """
    HTTP trigger to start ADLS to Spanner data transfer.

    Accepts a POST request with a JSON body containing all required configuration
    parameters, starts a new Durable Functions orchestration instance, and returns
    a check-status response so the caller can poll for completion.

    Args:
        req: HTTP request containing the JSON configuration body
        client: Durable orchestration client

    Returns:
        HTTP 202 response with polling URLs, or 400/500 on error
    """
    try:
        body = req.get_json()
    except Exception as e:
        logging.error(f"Invalid JSON body: {str(e)}")
        return func.HttpResponse("Invalid JSON body", status_code=400)

    try:
        instance_id = await client.start_new(
            "adls_to_spanner_orchestrator", None, body
        )
        response = client.create_check_status_response(req, instance_id)
        return response

    except Exception as e:
        logging.error(f"HTTP start failed: {str(e)}", exc_info=True)
        return func.HttpResponse(
            json.dumps({"error": str(e)}),
            mimetype="application/json",
            status_code=500,
        )

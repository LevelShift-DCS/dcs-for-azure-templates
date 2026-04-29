# dcsazure_PostgreSQL_to_PostgreSQL_discovery_pl
## Delphix Compliance Services (DCS) for Azure - PostgreSQL to PostgreSQL Discovery Pipeline

This pipeline will perform automated sensitive data discovery on your PostgreSQL DB Tables.

### Prerequisites

1. Configure the hosted metadata database and associated Azure SQL linked service (version `V2026.04.29.0`).
1. Configure the DCS for Azure REST linked service.
1. Configure the Azure Data Lake Storage (Gen 2) linked service for staging exported PostgreSQL DB data.
1. [Assign a managed identity with a Storage Blob Data Contributor role for the Data Factory instance within the storage account](External_document_link).
1. [Create an Azure Function app for exporting PostgreSQL DB data to Azure Data Lake Storage (ADLS)](External_document_link) (version `PostgreSQL_to_ADLS_V1`).
1. [Configure an Azure Key Vault for storing the ADLS access key and assign a managed identity with the Key Vault Secrets User role to the Azure Function](External_document_link).
1. [Configure an Azure Key Vault for storing the PostgreSQL DB access key and assign a managed identity with the Key Vault Secrets User role to the Azure Function](External_document_link).
1. [Deploy the Azure Function to the Function App created in the previous step](./PostgreSQL_to_ADLS/AzureFunctionDeployment.md).
1. [Configure the Azure Function Linked service](External_document_link).

### Importing
There are several linked services that will need to be selected in order to perform the profiling and data discovery of your PostgreSQL source data.

These linked service types are needed for the following steps:

`Azure Function` (PostgreSQL to ADLS) - Linked service associated with exporting PostgreSQL DB data to ADLS. This will be used for the following steps:
* Check If We Should Copy Data To ADLS (If Condition activity)

`Azure Data Lake Storage Gen2` (staging) - Linked service associated with the ADLS account used for staging PostgreSQL DB exports. This will be used for the following steps:
* dcsazure_PostgreSQL_to_PostgreSQL_ADLS_delimited_container_and_directory_discovery_ds (DelimitedText dataset),
* dcsazure_PostgreSQL_to_PostgreSQL_ADLS_delimited_data_discovery_df/SourceData1MillRowDataSampling (dataFlow),
* dcsazure_PostgreSQL_to_PostgreSQL_ADLS_delimited_header_file_schema_discovery_ds (DelimitedText dataset)

`Azure SQL` (metadata) - Linked service associated with your hosted metadata store. This will be used for the following steps:
* Set Source Metadata (Script activity),
* Check PostgreSQL To ADLS Status (If Condition activity),
* Check If We Should Update Copy State (If Condition activity),
* Update Discovery State (Stored procedure activity),
* Update Discovery State Failed (Stored procedure activity),
* Check If We Should Rediscover Data (If Condition activity),
* dcsazure_PostgreSQL_to_PostgreSQL_ADLS_delimited_metadata_discovery_ds (Azure SQL Database dataset),
* dcsazure_PostgreSQL_to_PostgreSQL_ADLS_delimited_data_discovery_df/MetadataStoreRead (dataFlow),
* dcsazure_PostgreSQL_to_PostgreSQL_ADLS_delimited_data_discovery_df/WriteToMetadataStore (dataFlow),
* Persist Metadata To Database (Stored procedure activity)

`REST` (DCS for Azure) - Linked service associated with calling DCS for Azure. This will be used for the following steps:
* dcsazure_PostgreSQL_to_PostgreSQL_ADLS_delimited_data_discovery_df (dataFlow)

### How It Works

* Check If We Should Copy Data To ADLS
  * Copy PostgreSQL Data to ADLS
    * Export documents from a PostgreSQL DB table to ADLS using an Azure Function
* Until PostgreSQL to ADLS Durable Function is Success
  * Poll the Azure Function execution status until the export completes
* Check PostgreSQL to ADLS Status
  * Validate that the export completed successfully, otherwise fail the pipeline
* Discover Sensitive Data
  * Check If We Should Rediscover Data
    * If we should, Mark Tables Undiscovered. This is done by updating the metadata store to indicate that tables have not had their sensitive data discovered
  * Identify Nested Schemas
    * Using the child pipeline `dcsazure_PostgreSQL_to_PostgreSQL_ADLS_delimited_container_and_directory_discovery_pl`, we collect all the identified schemas under the specified directory.
    * For each item in that list, identify if the schema of the files in that child directory is expected to be homogeneous.
  * Schema Discovery
    * For each of the directories with homogeneous schema, identify the schema for each file with one of the suffixes to scan, determine the structure of the file by calling the child `dcsazure_PostgreSQL_to_PostgreSQL_ADLS_delimited_file_discovery_pl` pipeline with the appropriate parameters.
  * Select Discovered Tables - In this case, we consider the table to be items with the same schema.
    * After the previous step, we query the database for all tables (file suffixes within each distinct path of the storage container) and perform profiling for sensitive data discovery in those files that have not yet been discovered.
  * ForEach Discovered Table
    * Each table that we've discovered needs to be profiled, the process for that is as follows:
      * Run the `dcsazure_PostgreSQL_to_PostgreSQL_ADLS_delimited_data_discovery_df` dataflow with the appropriate parameters.
* Set Source Metadata
  * After sensitive data discovery completes successfully, update the metadata store to enrich the discovered objects with PostgreSQL-specific source context, including source host, schema, table, and filter metadata, ensuring discovery results are accurately traceable.

### Variables

If you have configured your database using the metadata store scripts, these variables will not need editing. If you have customized your metadata store, then these variables may need editing.

* `METADATA_SCHEMA` - This is the schema to be used for storing metadata (default `dcsazure_metadata_store`)
* `METADATA_SCHEMA` - This is the schema to be used for storing metadata (default `dbo`)
* `METADATA_RULESET_TABLE` - This is the table to be used for storing the discovered ruleset (default `discovered_ruleset`)
* `DATASET` - This is used to identify data that belongs to this pipeline in the metadata store (default `POSTGRESQL`)
* `METADATA_EVENT_PROCEDURE_NAME` - This is the name of the procedure used to capture pipeline execution information and set discovery state (default `insert_adf_discovery_event`)
* `NUMBER_OF_ROWS_TO_PROFILE` - This is the number of rows selected for profiling (default `1000`)
* `COLUMNS_FROM_ADLS_FILE_STRUCTURE_PROCEDURE_NAME` - Stored procedure used to infer columns from delimited files (default `get_columns_from_delimited_file_structure_sp`)
* `STORAGE_ACCOUNT` - Azure Data Lake Storage account name (default `DCS_PLACEHOLDER`)
* `MAX_LEVELS_TO_RECURSE` - Maximum directory recursion depth (default `15`)
* `POSTGRESQL_TO_ADLS_BATCH_SIZE` - This is the number of rows per batch while copying the data from PostgreSQL database to ADLS (default `100000`)
* `POSTGRESQL_KEY_VAULT_NAME` – Name of the Azure Key Vault that stores the PostgreSQL DB access key (default `DCS_PLACEHOLDER`)
* `POSTGRESQL_SECRET_NAME` – Name of the secret in Key Vault containing the PostgreSQL DB access key (default `DCS_PLACEHOLDER`)
* `ADLS_SECRET_NAME` - Name of the secret in Key Vault containing the ADLS access key (default `DCS_PLACEHOLDER`)
* `POSTGRESQL_SSL_MODE` - SSL mode used for PostgreSQL connection (default `require`)
* `MAX_RESULTS` - Maximum number of results returned during ADLS discovery (default `5000`)
* `MSFT_API_VERSION` - Microsoft Storage API version used for blob operations (default `2021-08-06`)
* `MSFT_BLOB_TYPE` - Blob type used during staging operations (default `BlockBlob`)

### Parameters

* `P_POSTGRESQL_SOURCE_HOST` - String - PostgreSQL server hostname or endpoint
* `P_POSTGRESQL_SOURCE_PORT` - Integer - PostgreSQL server port
* `P_POSTGRESQL_SOURCE_DATABASE` - String - Source PostgreSQL database name
* `P_POSTGRESQL_SOURCE_SCHEMA` - String - Source PostgreSQL schema name
* `P_POSTGRESQL_SOURCE_TABLE` - String - Source PostgreSQL table name
* `P_POSTGRESQL_SOURCE_USERNAME` - String - Username used to connect to PostgreSQL
* `P_POSTGRESQL_FILTER_COLUMN_NAME` - String - Optional filter column used for partial export from PostgreSQL
* `P_POSTGRESQL_FILTER_COLUMN_VALUE` - Array - Optional filter values for the filter column
* `P_ADLS_CONTAINER_NAME` - String - Azure Data Lake Storage container name used for staging
* `P_REDISCOVER` - Bool - Specifies whether previously discovered data should be rediscovered (default `true`)
* `P_COPY_POSTGRESQL_DATA_TO_ADLS` - Bool - Specifies whether data should be copied from PostgreSQL to ADLS before discovery (default `true`)

### Notes

* When creating the Azure Function used for PostgreSQL DB export, choose the hosting plan based on data volume:
  * The default timeout for the Consumption plan is 10 minutes.
  * The default timeout for the Flex Consumption plan is 60 minutes.
  * For containers with millions of documents, it is recommended to use an App Service plan with at least 4 GB of memory.
    * This allows the function to run without time limits until all records are processed.
    * This approach is especially recommended when the target container has low RU provisioning or a very large number of records.
    * The Azure Function timeout is explicitly configured to **12 hours** using the `functionTimeout` setting to support large PostgreSQL DB containers.
* If the Azure Function fails with out-of-memory errors (exit code 137), adjust the `POSTGRESQL_TO_ADLS_BATCH_SIZE` to reduce memory pressure.
* Update the `POSTGRESQL_KEY_VAULT_NAME` and `POSTGRESQL_SECRET_NAME` variables to match the target PostgreSQL DB account before triggering the pipeline.
* To filter records from PostgreSQL DB, both `P_POSTGRESQL_FILTER_COLUMN_NAME` and `P_POSTGRESQL_FILTER_COLUMN_VALUE` must be provided.
* This pipeline operates at the schema/table level. When an array of `P_POSTGRESQL_FILTER_COLUMN_VALUE` is specified, only matching rows are exported to ADLS and included in discovery and masking.
* The `source_metadata` column in the `discovered_ruleset` table can be used to identify which filtered data is currently staged in ADLS prior to running the masking pipeline. For example:
  ```sql
  SELECT
      d.dataset,
      d.specified_database,
      d.specified_schema,
      d.identified_table,
      d.identified_column,
      pv.value AS partition_value
  FROM discovered_ruleset d
  CROSS APPLY OPENJSON(d.source_metadata, '$.partition_values') pv
  WHERE d.dataset = 'POSTGRESQL'
    AND d.specified_schema LIKE 'POSTGRESQL-DATABASE/POSTGRESQL-SCHEMA-NAME/POSTGRESQL-TABLE-NAME%';
  ```
* If the pipeline is rerun for the same source schema/table with a different set of `P_POSTGRESQL_FILTER_COLUMN_VALUE`, the data in ADLS is overwritten with the new data, and the corresponding metadata in the ruleset is updated.
* Historical information about previously discovered filtered chunks can be obtained from the `adf_events` log table.

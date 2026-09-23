# dcsazure_Spanner_to_Spanner_discovery_pl
## Delphix Compliance Services (DCS) for Azure - Spanner to Spanner Discovery Pipeline

This pipeline will perform automated sensitive data discovery on your Google Cloud Spanner tables.

### Prerequisites

1. Configure the hosted metadata database and associated Azure SQL linked service (version `V2026.02.25.0`).
1. Configure the DCS for Azure REST linked service.
1. Configure the Azure Data Lake Storage (Gen 2) linked service for staging exported Spanner data.
1. [Assign a managed identity with a Storage Blob Data Contributor role for the Data Factory instance within the storage account](https://help.delphix.com/dcs/current/content/docs/configure_adls_delimited_pipelines.htm).
1. [Create an Azure Function app for exporting Spanner data to Azure Data Lake Storage (ADLS)](https://help.delphix.com/dcs/current/content/docs/create_an_azure_function.htm) (version `Spanner_to_ADLS_V1`).
1. [Assign a managed identity with a Storage Blob Data Contributor role for the Azure Function instance within the storage account](https://help.delphix.com/dcs/current/content/docs/create_an_azure_function.htm).
1. [Configure an Azure Key Vault for storing the Spanner credentials secret and assign a managed identity with the Key Vault Secrets User role to the Azure Function](https://help.delphix.com/dcs/current/content/docs/configure_azure_function_access_to_spanner_secret_using_azure_key_vault.htm).
1. [Deploy the Azure Function to the Function App created in the previous step](./Spanner_to_ADLS/AzureFunctionDeployment.md).
1. [Configure the Azure Function Linked service](https://help.delphix.com/dcs/current/content/docs/linked_service_for_google_cloud_spanner_source.htm).

### Importing
There are several linked services that will need to be selected in order to perform the profiling and data discovery of your Spanner tables.

These linked service types are needed for the following steps:

`Azure Function` (Spanner to ADLS) - Linked service associated with exporting Spanner data to ADLS. This will be used for the following steps:
* Check If We Should Copy Data To ADLS (If Condition activity)

`Azure Data Lake Storage Gen2` (staging) - Linked service associated with the ADLS account used for staging Spanner exports. This will be used for the following steps:
* dcsazure_Spanner_to_Spanner_ADLS_delimited_container_and_directory_discovery_ds (DelimitedText dataset),
* dcsazure_Spanner_to_Spanner_ADLS_delimited_data_discovery_df/SourceData1MillRowDataSampling (dataFlow),
* dcsazure_Spanner_to_Spanner_ADLS_delimited_header_file_schema_discovery_ds (DelimitedText dataset)

`Azure SQL` (metadata) - Linked service associated with your hosted metadata store. This will be used for the following steps:
* Set Source Metadata (Script activity),
* Check Spanner To ADLS Status (If Condition activity),
* Check If We Should Update Copy State (If Condition activity),
* Update Discovery State (Stored procedure activity),
* Update Discovery State Failed (Stored procedure activity),
* Check If We Should Rediscover Data (If Condition activity),
* dcsazure_Spanner_to_Spanner_ADLS_delimited_metadata_discovery_ds (Azure SQL Database dataset),
* dcsazure_Spanner_to_Spanner_ADLS_delimited_data_discovery_df/MetadataStoreRead (dataFlow),
* dcsazure_Spanner_to_Spanner_ADLS_delimited_data_discovery_df/WriteToMetadataStore (dataFlow),
* Persist Metadata To Database (Stored procedure activity)

`REST` (DCS for Azure) - Linked service associated with calling DCS for Azure. This will be used for the following steps:
* dcsazure_Spanner_to_Spanner_ADLS_delimited_data_discovery_df (dataFlow)

### How It Works

* Check If We Should Copy Data To ADLS
  * Copy Spanner Data to ADLS
    * Export rows from a Spanner table to ADLS using an Azure Function
* Until Spanner To ADLS Durable Function Is Success
  * Poll the Azure Function execution status until the export completes
* Check Spanner to ADLS Status
  * Validate that the export completed successfully, otherwise fail the pipeline
* Discover Sensitive Data
  * Check If We Should Rediscover Data
    * If we should, Mark Tables Undiscovered. This is done by updating the metadata store to indicate that tables have not had their sensitive data discovered
  * Identify Nested Schemas
    * Using the child pipeline `dcsazure_Spanner_to_Spanner_ADLS_delimited_container_and_directory_discovery_pl`, we collect all the identified schemas under the specified directory.
    * For each item in that list, identify if the schema of the files in that child directory is expected to be homogeneous.
  * Schema Discovery
    * For each of the directories with homogeneous schema, identify the schema for each file with one of the suffixes to scan, determine the structure of the file by calling the child `dcsazure_Spanner_to_Spanner_ADLS_delimited_file_discovery_pl` pipeline with the appropriate parameters.
  * Select Discovered Tables - In this case, we consider the table to be items with the same schema.
    * After the previous step, we query the database for all tables (file suffixes within each distinct path of the storage container) and perform profiling for sensitive data discovery in those files that have not yet been discovered.
  * ForEach Discovered Table
    * Each table that we've discovered needs to be profiled, the process for that is as follows:
      * Run the `dcsazure_Spanner_to_Spanner_ADLS_delimited_data_discovery_df` dataflow with the appropriate parameters.
* Set Source Metadata
  * After sensitive data discovery completes successfully, update the metadata store to enrich the discovered objects with Spanner-specific source context, including filter column values, ensuring discovery results are accurately traceable.


### Variables

If you have configured your database using the metadata store scripts, these variables will not need editing. If you have customized your metadata store, then these variables may need editing.

* `METADATA_SCHEMA` - This is the schema to be used for storing metadata (default `dcsazure_metadata_store`)
* `METADATA_RULESET_TABLE` - This is the table to be used for storing the discovered ruleset (default `discovered_ruleset`)
* `DATASET` - This is used to identify data that belongs to this pipeline in the metadata store (default `SPANNER`)
* `METADATA_EVENT_PROCEDURE_NAME` - This is the name of the procedure used to capture pipeline execution information and set discovery state (default `insert_adf_discovery_event`)
* `NUMBER_OF_ROWS_TO_PROFILE` - This is the number of rows selected for profiling (default `1000`)
* `COLUMNS_FROM_ADLS_FILE_STRUCTURE_PROCEDURE_NAME` - Stored procedure used to infer columns from delimited files (default `get_columns_from_delimited_file_structure_sp`)
* `STORAGE_ACCOUNT` - Azure Data Lake Storage account name
* `MAX_LEVELS_TO_RECURSE` - Maximum directory recursion depth (default `10`)
* `SPANNER_TO_ADLS_BATCH_SIZE` - This is the number of rows per batch while copying the data from Spanner to ADLS (default `50000`)
* `SPANNER_KEY_VAULT_NAME` – Name of the Azure Key Vault that stores the Spanner credentials secret
* `SPANNER_SECRET_NAME` – Name of the secret in Key Vault containing the Spanner credentials

### Parameters

* `P_SPANNER_PROJECT_ID` - String - Google Cloud project ID that hosts the Spanner instance
* `P_SPANNER_INSTANCE_ID` - String - Google Cloud Spanner instance ID
* `P_SPANNER_SOURCE_DATABASE` - String - Spanner database name
* `P_SPANNER_SOURCE_TABLE` - String - Spanner table name
* `P_SPANNER_FILTER_COLUMN_NAME` - String - Optional column name used to filter rows during export
* `P_SPANNER_FILTER_COLUMN_VALUE` - Array - Optional filter values for the specified filter column
* `P_ADLS_CONTAINER_NAME` - String - Azure Data Lake Storage filesystem / container name
* `P_REDISCOVER` - Bool - Specifies if previously discovered data should be rediscovered (default `true`)
* `P_COPY_SPANNER_DATA_TO_ADLS` – Bool – Specifies whether data should be copied from Spanner to ADLS (default `true`)

### Notes

* When creating the Azure Function used for Spanner export, choose the hosting plan based on data volume:
  * The default timeout for the Consumption plan is 10 minutes.
  * The default timeout for the Flex Consumption plan is 60 minutes.
  * For tables with millions of rows, it is recommended to use an App Service plan with at least 4 GB of memory.
    * This allows the function to run without time limits until all records are processed.
    * This approach is especially recommended when the target table has a very large number of records.
    * The Azure Function timeout is explicitly configured to **12 hours** using the `functionTimeout` setting to support large Spanner tables.
* If the Azure Function fails with out-of-memory errors (exit code 137), adjust the `SPANNER_TO_ADLS_BATCH_SIZE` to reduce memory pressure.
* Update the `SPANNER_KEY_VAULT_NAME` and `SPANNER_SECRET_NAME` variables to match the target Spanner instance before triggering the pipeline.
* To filter rows during export, both `P_SPANNER_FILTER_COLUMN_NAME` and `P_SPANNER_FILTER_COLUMN_VALUE` must be provided.
* This pipeline operates at the table level. When an array of `P_SPANNER_FILTER_COLUMN_VALUE` is specified, only data matching those filter values is exported to ADLS and included in discovery.
* If the pipeline is rerun for the same table with a different set of `P_SPANNER_FILTER_COLUMN_VALUE`, the data in ADLS is overwritten with the new filter's data, and the corresponding filter metadata in the ruleset is updated.
* Historical information about previously discovered filter runs can be obtained from the `adf_events` log table.

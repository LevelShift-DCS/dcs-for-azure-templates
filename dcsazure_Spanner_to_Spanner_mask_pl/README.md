# dcsazure_Spanner_to_Spanner_mask_pl
## Delphix Compliance Services (DCS) for Azure - Spanner to Spanner Masking Pipeline

This pipeline will perform masking of your Google Cloud Spanner data.

### Prerequisites

1. Configure the hosted metadata database and associated Azure SQL linked service (version `V2026.02.25.0`).
1. Configure the DCS for Azure REST linked service.
1. Configure the Azure Data Lake Storage linked service associated with your ADLS source data.
1. Configure the Azure Data Lake Storage linked service associated with your ADLS sink data.
1. [Assign a managed identity with a Storage Blob Data Contributor role for the Data Factory instance within the storage account](https://help.delphix.com/dcs/current/content/docs/configure_adls_delimited_pipelines.htm).
1. [Create an Azure Function app for exporting Spanner data to Azure Data Lake Storage (ADLS)](https://help.delphix.com/dcs/current/content/docs/create_an_azure_function.htm) (version `ADLS_to_Spanner_V1`).
1. [Assign a managed identity with a Storage Blob Data Contributor role for the Azure Function instance within the storage account](https://help.delphix.com/dcs/current/content/docs/create_an_azure_function.htm).
1. [Configure an Azure Key Vault for storing the Spanner access key and assign a managed identity with the Key Vault Secrets User role to the Azure Function](https://help.delphix.com/dcs/current/content/docs/configure_azure_function_access_to_spanner_secret_using_azure_key_vault.htm).
1. [Deploy the Azure Function to the Function App created in the previous step](./Spanner_to_ADLS/AzureFunctionDeployment.md).
1. [Configure the Azure Function Linked service](https://help.delphix.com/dcs/current/content/docs/linked_service_for_google_cloud_spanner_source.htm).

### Importing
There are several linked services that will need to be selected in order to perform the masking of your Spanner data.

These linked service types are needed for the following steps:

`Azure Function` (ADLS to Spanner) – Linked service associated with loading masked data from ADLS into a Spanner database. This will be used for the following steps:
* Check If We Should Copy Data To Spanner (If Condition activity)

`Azure Data Lake Storage Gen2` (Source) – Linked service associated with the ADLS account used for staging Source Spanner data. This will be used for the following steps:
* dcsazure_Spanner_to_Spanner_ADLS_delimited_filter_test_utility_df/Source (dataFlow),
* dcsazure_Spanner_to_Spanner_ADLS_delimited_container_and_directory_mask_ds (DelimitedText dataset),
* dcsazure_Spanner_to_Spanner_ADLS_delimited_unfiltered_mask_df/Source (dataFlow),
* dcsazure_Spanner_to_Spanner_ADLS_delimited_copy_df/Source (dataFlow)

`Azure Data Lake Storage Gen2` (Sink) – Linked service associated with the ADLS account used for staging Sink Spanner data. This will be used for the following steps:
* dcsazure_Spanner_to_Spanner_ADLS_delimited_filter_test_utility_df/Sink (dataFlow),
* dcsazure_Spanner_to_Spanner_ADLS_delimited_unfiltered_mask_df/Sink (dataFlow),
* dcsazure_Spanner_to_Spanner_ADLS_delimited_copy_df/Sink (dataFlow)

`Azure SQL` (metadata) – Linked service associated with your hosted metadata store. This will be used for the following steps:
* Check ADLS To Spanner Status (If Condition activity),
* Update Logs If Copy Data To ADLS Is True (If Condition activity),
* Check If We Should Reapply Mapping (If Condition activity),
* For Each Table To Mask (ForEach activity),
* For Each Table With No Masking (ForEach activity),
* If Copy Via Dataflow (If Condition activity),
* If Copy Via Dataflow (If Condition activity),
* If Copy Via Dataflow (If Condition activity),
* If Copy Via Dataflow (If Condition activity),
* Check If At Least One Column Is Masked (If Condition activity),
* dcsazure_Spanner_to_Spanner_ADLS_delimited_metadata_mask_ds (Azure SQL Database dataset)

`REST` (DCS for Azure) – Linked service associated with calling DCS for Azure. This will be used for the following steps:
* dcsazure_Spanner_to_Spanner_ADLS_delimited_unfiltered_mask_df (dataFlow)

### How It Works

* Execute ADLS Masking Pipeline
  * Check If We Should Reapply Mapping
    * If we should, Mark Table Mapping Incomplete. This is done by updating the metadata store to indicate that tables have not had their mapping applied
  * Select Directories We Should Purge
    * Select sink directories with an incomplete mapping and based on the value of P_TRUNCATE_SINK_BEFORE_WRITE, create a list of directories that we should purge
      * For Each Directory To Purge:
        * Check For Files
        * If the sink directory exists, delete everything in that directory
  * Select Tables Without Required Masking. This is done by querying the metadata store.
    * Filter If Copy Unmask Enabled. This is done by applying a filter based on the value of P_COPY_UNMASKED_TABLES
      * For Each Table With No Masking. Provided we have any rows left after applying the filter
        * If Copy Via Dataflow - based on the value of P_COPY_USE_DATAFLOW
          * If the data flow is to be used for copy then call `dcsazure_Spanner_to_Spanner_ADLS_delimited_copy_df`
            * Update the mapped status based on the success of this dataflow, and fail accordingly
          * If the data flow is not to be used for copy, then use a copy activity
            * Update the mapped status based on the success of this dataflow, and fail accordingly
  * Select Tables That Require Masking. This is done by querying the metadata store. This will provide a list of tables that need masking, and if they need to be masked leveraging conditional algorithms, the set of required filters.
    * Check If At Least One Column Is Masked. Fail the pipeline if no columns are configured for masking.
    * For Each Table To Mask
      * Get Source Metadata Mask. Retrieve metadata for the source table.
      * Lookup Masking Parameters. Retrieve the masking parameters for this table.
      * Perform Masking Per Table No Filter
        * Call the `dcsazure_Spanner_to_Spanner_ADLS_delimited_unfiltered_mask_df` data flow, passing in parameters as generated by the Lookup Masking Parameters activity
        * Update the masked status based on the success of this dataflow, and fail accordingly
  * Note that there is a deactivated activity Test Filter Condition that exists in order to support importing the filter test utility dataflow, this is making it easier to test writing filter conditions leveraging a dataflow debug session
* Check If We Should Copy Data To Spanner
  * Copy ADLS Data To Spanner
    * Load masked documents from ADLS into the Spanner database using an Azure Function (`ADLS_to_Spanner_V1`)
* Until ADLS To Spanner Durable Function Is Success
  * Poll the Azure Function execution status until the load completes
* Check ADLS To Spanner Status
  * Validate that the load completed successfully, otherwise fail the pipeline
* Delete Masked Staging Data
  * If P_DELETE_STAGING_AFTER_LOAD is true, purge the sink ADLS directory after the data has been successfully loaded into Spanner

### Variables

If you have configured your database using the metadata store scripts, these variables will not need editing. If you
have customized your metadata store, then these variables may need editing.

* `METADATA_SCHEMA` – Schema used for storing metadata (default `dcsazure_metadata_store`)
* `METADATA_RULESET_TABLE` – Table used for storing discovered rulesets (default `discovered_ruleset`)
* `METADATA_SOURCE_TO_SINK_MAPPING_TABLE` – Table defining source-to-sink mappings (default `adf_data_mapping`)
* `METADATA_ADF_TYPE_MAPPING_TABLE` – Table mapping dataset data types to ADF data types (default `adf_type_mapping`)
* `TARGET_BATCH_SIZE` – Target number of rows per batch during masking (default `50000`)
* `DATASET` – Dataset identifier used in the metadata store (default `SPANNER`)
* `METADATA_EVENT_PROCEDURE_NAME` – Stored procedure used to capture masking execution events and update masking state (default `insert_adf_masking_event`)
* `METADATA_MASKING_PARAMS_PROCEDURE_NAME` – Stored procedure used to generate Spanner masking parameters (default `generate_masking_parameters`)
* `COLUMN_WIDTH_ESTIMATE` – Estimated column width used for batch size calculation when schema width is unavailable (default `1000`)
* `STORAGE_ACCOUNT` – Azure Data Lake Storage account name used for staging masked data
* `ADLS_TO_SPANNER_BATCH_SIZE` – This is the number of rows per batch while loading the masked data from ADLS into the Spanner database (default `100000`)
* `SPANNER_KEY_VAULT_NAME` – Name of the Azure Key Vault that stores the Spanner access key
* `SPANNER_SECRET_NAME` – Name of the secret in Key Vault containing the Spanner access key
* `ADLS_SECRET_NAME` – Name of the secret in Key Vault containing the ADLS storage account key

### Parameters

* `P_SPANNER_SOURCE_DATABASE` – String – Source Spanner database name
* `P_SPANNER_SINK_DATABASE` – String – Target Spanner database name for masked data
* `P_SPANNER_TABLE` – String – Spanner table name to mask
* `P_SPANNER_PROJECT_ID` – String – Google Cloud project ID associated with the Spanner instance
* `P_SPANNER_INSTANCE_ID` – String – Spanner instance ID
* `P_ADLS_SOURCE_CONTAINER` – String – ADLS filesystem/container for unmasked data
* `P_ADLS_SINK_CONTAINER` – String – ADLS filesystem/container for masked data
* `P_FAIL_ON_NONCONFORMANT_DATA` – Bool – Fail pipeline if non-conformant data is encountered (default `true`)
* `P_COPY_UNMASKED_TABLES` – Bool – Copy data even when no masking rules are defined (default `true`)
* `P_COPY_USE_DATAFLOW` – Bool – Use dataflow instead of copy activity when copying data (default `false`)
* `P_TRUNCATE_SINK_BEFORE_WRITE` – Bool – Truncate target Spanner table before writing masked data (default `true`)
* `P_REAPPLY_MAPPING` – Bool – Reapply source-to-sink mapping before masking (default `true`)
* `P_COPY_ADLS_DATA_TO_SPANNER` – Bool – Specifies whether masked data should be loaded from ADLS into the Spanner database (default `true`)
* `P_DELETE_STAGING_AFTER_LOAD` – Bool – Delete the masked staging data from ADLS after successfully loading it into Spanner (default `false`)

### Notes

* Update the `SPANNER_KEY_VAULT_NAME` and `SPANNER_SECRET_NAME` variables to match the target Spanner instance before triggering the pipeline.
* Update the `ADLS_SECRET_NAME` variable to match the ADLS storage account key secret in Key Vault before triggering the pipeline.
* The `P_SPANNER_PROJECT_ID` and `P_SPANNER_INSTANCE_ID` parameters must match the Google Cloud project and Spanner instance that hosts both the source and sink databases.
* The Spanner table name must be the same in both the source and sink databases for the masking pipeline to function correctly.
* Ensure that all schemas associated with the Spanner table are added to the `adf_data_mapping` table before triggering the masking pipeline.
* The `source_metadata` column in the `discovered_ruleset` table can be used to determine which partition data is currently staged in ADLS prior to running the masking pipeline. For example:
  ```sql
  SELECT
      d.dataset,
      d.specified_database,
      d.specified_schema,
      d.identified_table,
      d.identified_column,
      pv.value AS partition_value
  FROM <METADATA_SCHEMA>.<METADATA_RULESET_TABLE> d
  CROSS APPLY OPENJSON(d.source_metadata, '$.partition_values') pv
  WHERE d.dataset = 'SPANNER'
    AND d.specified_schema LIKE 'SPANNER-DATABASE/SPANNER-TABLE-NAME%';
  ```
* If a column exists in some rows but is missing in others, the pipeline will still include that column in the masked output, populating `null` values for rows where the column was not originally present.
* Conditional masking is not supported by this template.
* Setting `P_DELETE_STAGING_AFTER_LOAD` to `true` will remove masked staging files from ADLS after a successful load into Spanner. This is useful for minimizing storage costs but means staging data cannot be reused for a retry without re-running the masking step.
* When creating the Azure Function used for loading data from ADLS to Spanner, choose the hosting plan based on data volume:
  * The default timeout for the Consumption plan is 10 minutes.
  * The default timeout for the Flex Consumption plan is 60 minutes.
  * For tables with millions of rows, it is recommended to use an App Service plan with at least 4 GB of memory.
    * This allows the function to run without time limits until all records are processed.
    * This approach is especially recommended when the target Spanner instance has low write throughput provisioning or a very large number of rows.

### Limitations and Workarounds

* **ARRAY and STRUCT column masking**
  * When a Spanner table contains columns of type `ARRAY` or `STRUCT`, the discovery and masking pipeline treats the entire value as a single string during the ADLS staging phase.
  * Applying a standard string-based masking algorithm to an `ARRAY<STRING>` column results in a single masked string, causing the original array structure and data type to be lost when data is loaded back into Spanner.

* **Reason for the limitation**
  * `ARRAY` columns do not contain explicit keys and are indexed only by position.
  * During flattening to a delimited format for ADLS staging, there is no reliable way to map positional array elements to distinct columns.
  * As a result, individual array elements cannot be independently discovered or masked using standard DCS templates.
  * `STRUCT` columns present a similar challenge: nested fields are serialized as a composite string during staging, making field-level masking unavailable without custom decomposition logic.

* **Workaround**
  * When masking `ARRAY<STRING>` columns, use an algorithm that preserves the overall structure of the value.
  * The built-in `dlpx-core:CM Alpha-Numeric` algorithm can be used to mask array elements without relying on schema-aware decomposition.
  * Alternatively, a custom masking algorithm can be created using a regex-based approach to decompose the array and apply masking to individual elements while preserving the array format.
  * Regex-based decomposition requires an upper bound on the expected number of elements in the array, as capture groups must be defined in advance.
  * If the array contains fewer elements than expected, empty capture groups may cause the algorithm to fail.
  * If the array contains more elements than defined capture groups, additional values may not be masked and the algorithm may fall back or fail.
  * This workaround is suitable only when the maximum number of elements in the array is known and bounded.
  * For `STRUCT` columns, consider pre-processing the data to flatten nested fields into separate columns before running the masking pipeline, then re-assembling the struct after masking.

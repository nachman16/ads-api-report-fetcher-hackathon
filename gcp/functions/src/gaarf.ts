/**
 * Copyright 2022 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *      https://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

import fs from 'fs';
import {
  AdsQueryExecutor,
  BigQueryWriter,
  BigQueryWriterOptions,
  getFileContent,
  GoogleAdsApiClient,
  loadAdsConfigYaml,
} from 'google-ads-api-report-fetcher';

import type {HttpFunction} from '@google-cloud/functions-framework/build/src/functions';
import express from 'express';
import {GoogleAdsApiConfig} from 'google-ads-api-report-fetcher/src/lib/ads-api-client';
import {getAdsConfig, getScript} from './utils';

export const main: HttpFunction = async (
  req: express.Request,
  res: express.Response
) => {
  console.log(req.body);
  console.log(req.query);

  // prepare Ads API parameters
  const adsConfig: GoogleAdsApiConfig = await getAdsConfig(req);
  console.log('Ads API config:');
  const {refresh_token, ...ads_config_wo_token} = adsConfig;
  console.log(ads_config_wo_token);
  if (!adsConfig.developer_token || !adsConfig.refresh_token) {
    throw new Error('Ads API configuration is not complete.');
  }

  const projectId = req.query.bq_project_id || process.env.PROJECT_ID;
  if (!projectId)
    throw new Error(
      "Project id is not specified in either 'bq_project_id' query argument or PROJECT_ID envvar"
    );
  const dataset = req.query.bq_dataset || process.env.DATASET;
  if (!dataset)
    throw new Error(
      "Dataset is not specified in either 'bq_dataset' query argument or DATASET envvar"
    );
  const customerId = req.query.customer_id || adsConfig.customer_id;
  if (!customerId)
    throw new Error(
      "Customer id is not specified in either 'customer_id' query argument or google-ads.yaml"
    );

  const ads_client = new GoogleAdsApiClient(adsConfig, <string>customerId);
  // TODO: support CsvWriter and output path to GCS
  // (csv.destination_folder=gs://bucket/path)

  const singleCustomer = req.query.single_customer;
  const body = req.body || {};
  const macroParams = body.macro;
  const bq_writer_options: BigQueryWriterOptions = {
    datasetLocation: <string>req.query.bq_dataset_location,
  };

  const {queryText, scriptName} = await getScript(req);
  let customers: string[];
  if (singleCustomer) {
    console.log(
      `[${scriptName}] Executing for a single customer id: ${customerId}`
    );
    customers = [<string>customerId];
    bq_writer_options.noUnionView = true;
  } else {
    console.log(`[${scriptName}] Fetching customer ids`);
    customers = await ads_client.getCustomerIds();
    console.log(`[${scriptName}] Customers to process (${customers.length}):`);
    console.log(customers);
  }

  const executor = new AdsQueryExecutor(ads_client);
  const writer = new BigQueryWriter(
    <string>projectId,
    <string>dataset,
    bq_writer_options
  );

  const result = await executor.execute(
    scriptName,
    queryText,
    customers,
    macroParams,
    writer
  );

  console.log(`[${scriptName}] Cloud Function compeleted`);
  // we're returning a map of customer to number of rows
  res.json(result);
  res.end();
};

# Copyright 2022 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Module for executing Gaarf queries and writing them to local/remote.


Module defines two major classes:
    * AdsReportFetcher - to perform fetching data from Ads API, parsing it
      and returning GaarfReport.
    * AdsQueryExecutor - to perform fetching data from Ads API in a form of
      GaarfReport and saving it to local/remote storage.
"""
from __future__ import annotations

import enum
import importlib
import itertools
import logging
import warnings
from collections.abc import MutableSequence
from collections.abc import Sequence
from concurrent import futures
from typing import Any
from typing import Generator

from gaarf import api_clients
from gaarf import builtin_queries
from gaarf import exceptions
from gaarf import parsers
from gaarf import query_editor
from gaarf import report
from gaarf.io.writers import abs_writer
from gaarf.io.writers import console_writer
from google.ads.googleads import errors as googleads_exceptions
from google.api_core import exceptions as google_exceptions

google_ads_service = importlib.import_module(
    f'google.ads.googleads.{api_clients.GOOGLE_ADS_API_VERSION}.'
    'services.types.google_ads_service')

logger = logging.getLogger(__name__)


class OptimizeStrategy(enum.Enum):
    """Specifies how to parse response from Google Ads API."""
    NONE = 1
    BATCH = 2
    PROTOBUF = 3
    BATCH_PROTOBUF = 4


class AdsReportFetcher:
    """Class responsible for getting data from Ads API.

    Attributes:
        api_client: a client used for connecting to Ads API.
    """

    def __init__(self,
                 api_client: api_clients.BaseClient,
                 customer_ids: Sequence[str] | None = None) -> None:
        self.api_client = api_client
        if customer_ids:
            warnings.warn(
                '`AdsReportFetcher` will deprecate passing `customer_ids` to `__init__` method. '
                'Consider passing list of customer_ids to `AdsReportFetcher.fetch` method',
                category=DeprecationWarning,
                stacklevel=3)
            self.customer_ids = [
                customer_ids
            ] if not isinstance(customer_ids, list) else customer_ids

    def expand_mcc(self,
                   customer_ids: str | MutableSequence,
                   customer_ids_query: str | None = None) -> list[str]:
        return self._get_customer_ids(customer_ids, customer_ids_query)

    def fetch(self,
              query_specification: str | query_editor.QueryElements,
              customer_ids: list[str] | str | None = None,
              customer_ids_query: str | None = None,
              expand_mcc: bool = False,
              args: dict[str, Any] | None = None,
              optimize_strategy: str = 'NONE') -> report.GaarfReport:
        """Fetches data from Ads API based on query_specification.

        Args:
            query_specification: Query text that will be passed to Ads API
                alongside column_names, customizers and virtual columns.
            customer_ids: Account(s) for from which data should be fetched.
            custom_query: GAQL query used to reduce the number of customer_ids.
            expand_mcc: Whether to perform expansion of root customer_ids
                into leaf accounts.
            args: Arguments that need to be passed to the query
            optimize_strategy: strategy for speeding up query execution
                ("NONE", "PROTOBUF", "BATCH", "BATCH_PROTOBUF").

        Returns:
            GaarfReport with results of query execution.

        Raises:
            GaarfExecutorException:
                When customer_ids are not provided or Ads API returned error.
            GaarfBuiltInQueryException:
                When built-in query cannot be found in the registry.
        """

        if isinstance(self.api_client, api_clients.GoogleAdsApiClient):
            if not customer_ids:
                warnings.warn(
                    '`AdsReportFetcher` will require passing `customer_ids` '
                    'to `fetch` method.',
                    category=DeprecationWarning,
                    stacklevel=3)
                if hasattr(self, 'customer_ids'):
                    if not self.customer_ids:
                        raise exceptions.GaarfExecutorException(
                            'Please specify add `customer_ids` to '
                            '`fetch` method')
                    customer_ids = self.customer_ids
            else:
                if expand_mcc:
                    customer_ids = self.expand_mcc(customer_ids,
                                                   customer_ids_query)
                customer_ids = [
                    customer_ids
                ] if not isinstance(customer_ids, list) else customer_ids
        else:
            customer_ids = []
        total_results: list[list[tuple]] = []
        if not isinstance(query_specification, query_editor.QueryElements):
            query_specification = query_editor.QuerySpecification(
                text=str(query_specification),
                args=args,
                api_version=self.api_client.api_version).generate()

        if query_specification.is_builtin_query:
            if not (builtin_report := builtin_queries.BUILTIN_QUERIES.get(
                    query_specification.query_title)):
                raise exceptions.GaarfBuiltInQueryException(
                    'Cannot find the built-in query '
                    f'"{query_specification.title}"')
            return builtin_report(self, accounts=customer_ids)
        optimize_strategy = OptimizeStrategy[optimize_strategy]
        parser = parsers.GoogleAdsRowParser(query_specification)
        for customer_id in customer_ids:
            logger.debug('Running query %s for customer_id %s',
                         query_specification.query_title, customer_id)
            try:
                results = self._parse_ads_response(query_specification,
                                                   customer_id, parser,
                                                   optimize_strategy)
                total_results.extend(results)
                if query_specification.is_constant_resource:
                    logger.debug('Constant resource query: running only once')
                    break
            except googleads_exceptions.GoogleAdsException as e:
                logger.error('Cannot execute query %s for %s',
                             query_specification.query_title, customer_id)
                logger.error(str(e))
                raise exceptions.GaarfExecutorException
        if not total_results:
            results_placeholder = [
                parser.parse_ads_row(self.api_client.google_ads_row)
            ]
            if not isinstance(self.api_client, api_clients.BaseClient):
                logger.warning(
                    'Query %s generated zero results, '
                    'using placeholders to infer schema',
                    query_specification.query_title)
        else:
            results_placeholder = []
        return report.GaarfReport(
            results=total_results,
            column_names=query_specification.column_names,
            results_placeholder=results_placeholder,
            query_specification=query_specification)

    def _parse_ads_response(
        self,
        query_specification: query_editor.QueryElements,
        customer_id: str,
        parser: parsers.GoogleAdsRowParser,
        optimize_strategy: OptimizeStrategy = OptimizeStrategy.NONE
    ) -> list[list[tuple]]:
        """Parses response returned from Ads API request.

        Args:
            query_specification:
                Query text that will be passed to Ads API
                alongside column_names, customizers and virtual columns.
            customer_id:
                Account for which data should be requested.
            parser:
                An instance of parser class that transforms each row from
                request into desired format.
            optimize_strategy:
                Strategy for speeding up query execution
                ("NONE", "PROTOBUF", "BATCH", "BATCH_PROTOBUF").

        Returns:
            Parsed rows for the whole response.

        Raises:
            google_exceptions.InternalServerError:
                When data cannot be fetched from Ads API.
        """
        logger.debug('Getting response for query %s for customer_id %s',
                     query_specification.query_title, customer_id)
        try:
            response = self.api_client.get_response(
                entity_id=str(customer_id),
                query_text=query_specification.query_text,
                query_title=query_specification.query_title)
        except google_exceptions.InternalServerError:
            logging.error('Cannot fetch data from API for query "%s" 3 times',
                          query_specification.query_title)
            raise

        if optimize_strategy in (OptimizeStrategy.BATCH,
                                 OptimizeStrategy.BATCH_PROTOBUF):
            logger.warning('Running gaarf in an optimized mode')
            logger.warning('Optimize strategy is %s', optimize_strategy.name)
        if optimize_strategy in (OptimizeStrategy.BATCH,
                                 OptimizeStrategy.BATCH_PROTOBUF):
            return self._parse_ads_response_in_batches(
                response,
                query_specification,
                customer_id,
                parser,
            )
        return self._parse_ads_response_sequentially(response,
                                                     query_specification,
                                                     customer_id, parser)

    def _parse_ads_response_in_batches(
            self, response: google_ads_service.SearchGoogleAdsResponse,
            query_specification: query_editor.QueryElements, customer_id: str,
            parser: parsers.GoogleAdsRowParser) -> list[list]:
        """Parses response returned from Ads API request in parallel batches.

        Args:
            response: Google Ads API response.
            query_specification:
                Query text that will be passed to Ads API
                alongside column_names, customizers and virtual columns.
            customer_id:
                Account for which data are parsed.
            parser:
                An instance of parser class that transforms each row from
                request into desired format.

        Returns:
            Parsed rows for the whole response.
        """
        parsed_batches = []
        with futures.ThreadPoolExecutor() as executor:
            future_to_batch = {}
            for batch in response:
                future_to_batch[executor.submit(self._parse_batch, parser,
                                                batch.results)] = batch.results
            for i, future in enumerate(
                    futures.as_completed(future_to_batch), start=1):
                logger.debug('Parsed batch %d for query %s for customer_id %s',
                             i, query_specification.query_title, customer_id)
                parsed_batch = future.result()
                parsed_batches.append(parsed_batch)
        return list(itertools.chain.from_iterable(parsed_batches))

    def _parse_ads_response_sequentially(
            self, response: google_ads_service.SearchGoogleAdsResponse,
            query_specification: query_editor.QueryElements, customer_id: str,
            parser: parsers.GoogleAdsRowParser) -> list[list]:
        """Parses response returned from Ads API request sequentially.

        Args:
            response: Google Ads API response.
            query_specification:
                Query text that will be passed to Ads API
                alongside column_names, customizers and virtual columns.
            customer_id:
                Account for which data are parsed.
            parser:
                An instance of parser class that transforms each row from
                request into desired format.

        Returns:
            Parsed rows for the whole response.
        """
        total_results: list[list] = []
        logger.debug('Iterating over response for query %s for customer_id %s',
                     query_specification.query_title, customer_id)
        for batch in response:
            logger.debug('Parsing batch for query %s for customer_id %s',
                         query_specification.query_title, customer_id)

            results = self._parse_batch(parser, batch.results)
            total_results.extend(list(results))
        return total_results

    def _parse_batch(
        self, parser: parsers.GoogleAdsRowParser,
        batch: Sequence[google_ads_service.GoogleAdsRow]
    ) -> Generator[list, None, None]:
        """Parse reach row from batch of Ads API response.

        Args:
            parser:
                An instance of parser class that transforms each row from
                request into desired format.
            batch:
                Sequence of GoogleAdsRow that needs to be parsed.
        Yields:
            Parsed rows for a batch.
        """

        for row in batch:
            yield parser.parse_ads_row(row)

    def _get_customer_ids(self,
                          seed_customer_ids: str | MutableSequence,
                          customer_ids_query: str | None = None) -> list[str]:
        """Gets list of customer_ids from an MCC account.

        Args:
            customer_ids: MCC account_id(s).
            custom_query: GAQL query used to reduce the number of customer_ids.
        Returns:
            All customer_ids from MCC satisfying the condition.
        """

        query = """
        SELECT customer_client.id FROM customer_client
        WHERE customer_client.manager = FALSE
        AND customer_client.status = ENABLED
        """
        query_specification = query_editor.QuerySpecification(query).generate()
        if not isinstance(seed_customer_ids, MutableSequence):
            seed_customer_ids = seed_customer_ids.split(',')
        child_customer_ids = self.fetch(query_specification,
                                        seed_customer_ids).to_list()
        if customer_ids_query:
            query_specification = query_editor.QuerySpecification(
                customer_ids_query).generate()
            child_customer_ids = self.fetch(query_specification,
                                            child_customer_ids)
            child_customer_ids = [
                row[0] if isinstance(row, report.GaarfRow) else row
                for row in child_customer_ids
            ]

        child_customer_ids = list(
            set([
                customer_id for customer_id in child_customer_ids
                if customer_id != 0
            ]))

        return child_customer_ids


class AdsQueryExecutor:
    """Class responsible for getting data from Ads API and writing it to
        local/remote storage.

    Attributes:
        api_client: a client used for connecting to Ads API.
    """

    def __init__(self, api_client: api_clients.BaseClient) -> None:
        """Initializes QueryExecutor.

        Args:
            api_client: a client used for connecting to Ads API.
        """
        self.api_client = api_client

    @property
    def report_fetcher(self) -> AdsReportFetcher:
        """Initializes AdsReportFetcher to get data from Ads API."""
        return AdsReportFetcher(self.api_client)

    def execute(self,
                query_text: str,
                query_name: str,
                customer_ids: list[str] | str,
                writer_client: abs_writer.AbsWriter = console_writer
                .ConsoleWriter(),
                args: dict[str, Any] | None = None,
                optimize_performance: str = 'NONE') -> None:
        """Reads query, extract results and stores them in a specified location.

        Args:
            query_text: Text for the query.
            customer_ids: All accounts for which query will be executed.
            writer_client: Client responsible for writing data to local/remote
                location.
            args: Arguments that need to be passed to the query
            optimize_strategy: strategy for speeding up query execution
                ("NONE", "PROTOBUF", "BATCH", "BATCH_PROTOBUF")
        """

        query_specification = query_editor.QuerySpecification(
            query_text, query_name, args,
            self.report_fetcher.api_client.api_version).generate()
        results = self.report_fetcher.fetch(
            query_specification=query_specification,
            customer_ids=customer_ids,
            optimize_strategy=optimize_performance)
        logger.debug('Start writing data for query %s via %s writer',
                     query_specification.query_title, type(writer_client))
        writer_client.write(results, query_specification.query_title)
        logger.debug('Finish writing data for query %s via %s writer',
                     query_specification.query_title, type(writer_client))

    def expand_mcc(self,
                   customer_ids: str | MutableSequence,
                   customer_ids_query: str | None = None) -> list[str]:
        """Performs Manager account(s) expansion to child accounts.

        Args:
            customer_ids:
                Manager account(s) to be expanded.
            customer_ids_query:
                Gaarf query to limit the expansion only to accounts
                satisfying the condition.

        Returns:
            All child accounts under provided customer_ids.
        """
        return self.report_fetcher._get_customer_ids(customer_ids,
                                                     customer_ids_query)

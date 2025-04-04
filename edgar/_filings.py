import itertools
import json
import pickle
import re
import webbrowser
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
from functools import cached_property
from io import BytesIO
from os import PathLike
from pathlib import Path
from typing import Tuple, List, Dict, Union, Optional, Any, cast

import httpx
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pa_csv
import pyarrow.parquet as pq
from bs4 import BeautifulSoup
from fastcore.parallel import parallel
from rich import box
from rich.columns import Columns
from rich.console import Group
from rich.panel import Panel
from rich.status import Status
from rich.table import Table
from rich.text import Text

from edgar._markdown import text_to_markdown
from edgar._party import Address
from edgar.attachments import FilingHomepage, Attachment, Attachments, AttachmentServer
from edgar.core import (log, display_size, sec_edgar,
                        filter_by_date,
                        filter_by_form,
                        filter_by_cik,
                        filter_by_exchange,
                        filter_by_ticker,
                        filter_by_accession_number,
                        listify,
                        is_start_of_quarter,
                        has_html_content,
                        InvalidDateException,
                        IntString,
                        current_year_and_quarter,
                        Years,
                        Quarters,
                        YearAndQuarter,
                        YearAndQuarters,
                        quarters_in_year,
                        filing_date_to_year_quarters,
                        DataPager,
                        PagingState)
from edgar.files.html import Document
from edgar.files.html_documents import get_clean_html
from edgar.files.htmltools import html_sections
from edgar.files.markdown import to_markdown
from edgar.headers import FilingDirectory, IndexHeaders
from edgar.httprequests import download_file, download_text, download_text_between_tags
from edgar.httprequests import get_with_retry
from edgar.reference import describe_form
from edgar.reference.tickers import Exchange
from edgar.reference.tickers import find_ticker
from edgar.richtools import repr_rich, print_rich, rich_to_text
from edgar.search import BM25Search, RegexSearch
from edgar.sgml import FilingSGML, Reports, Statements, FilingHeader
from edgar.storage import local_filing_path, is_using_local_storage
from edgar.xbrl import XBRLData, XBRLInstance, get_xbrl_object
from edgar.xmltools import child_text

""" Contain functionality for working with SEC filing indexes and filings

The module contains the following functions

- `get_filings(year, quarter, index)`

"""

__all__ = [
    'Filing',
    'Filings',
    'get_filings',
    'FilingHeader',
    'PagingState',
    'Attachment',
    'Attachments',
    'FilingHomepage',
    'CurrentFilings',
    'available_quarters',
    'get_current_filings',
    'get_by_accession_number',
    'filing_date_to_year_quarters'
]

full_index_url = "https://www.sec.gov/Archives/edgar/full-index/{}/QTR{}/{}.{}"
daily_index_url = "https://www.sec.gov/Archives/edgar/daily-index/{}/QTR{}/{}.{}.idx"

filing_homepage_url_re = re.compile(f"{sec_edgar}/data/[0-9]{1,}/[0-9]{10}-[0-9]{2}-[0-9]{4}-index.html")

full_or_daily = ['daily', 'full']
index_types = ['form', 'company', 'xbrl']
file_types = ['gz', 'idx']

form_index = "form"
xbrl_index = "xbrl"
company_index = "company"

max_concurrent_http_connections = 10

accession_number_re = re.compile(r"\d{10}-\d{2}-\d{6}$")

xbrl_document_types = ['XBRL INSTANCE DOCUMENT', 'XBRL INSTANCE FILE', 'EXTRACTED XBRL INSTANCE DOCUMENT']


def is_valid_filing_date(filing_date: str) -> bool:
    if ":" in filing_date:
        # Check for only one colon
        if filing_date.count(":") > 1:
            return False
        start_date, end_date = filing_date.split(":")
        if start_date:
            if not is_valid_date(start_date):
                return False
        if end_date:
            if not is_valid_date(end_date):
                return False
    else:
        if not is_valid_date(filing_date):
            return False

    return True


def is_valid_date(date_str: str, date_format: str = "%Y-%m-%d") -> bool:
    pattern = r"^\d{4}-\d{2}-\d{2}$"
    if not re.match(pattern, date_str):
        return False

    try:
        datetime.strptime(date_str, date_format)
        return True
    except ValueError:
        return False


def get_previous_quarter(year, quarter) -> Tuple[int, int]:
    # Given a year and quarter return the previous quarter
    if quarter == 1:
        return year - 1, 4
    else:
        return year, quarter - 1


def available_quarters() -> YearAndQuarters:
    """
    Get a list of year and quarter tuples
    :return:
    """
    current_year, current_quarter = current_year_and_quarter()
    start_quarters = [(1994, 3), (1994, 4)]
    in_between_quarters = list(itertools.product(range(1995, current_year), range(1, 5)))
    end_quarters = list(itertools.product([current_year], range(1, current_quarter + 1)))
    return start_quarters + in_between_quarters + end_quarters


def expand_quarters(year: Union[int, List[int]],
                    quarter: Optional[Union[int, List[int]]] = None) -> YearAndQuarters:
    """
    Expand the list of years and a list of quarters to a full list of tuples covering the full range
    :param year: The year or years
    :param quarter: The quarter or quarters
    :return:
    """
    years = listify(year)
    quarters = listify(quarter) if quarter else quarters_in_year
    return [yq
            for yq in itertools.product(years, quarters)
            if yq in available_quarters()
            ]


class FileSpecs:
    """
    A specification for a fixed width file
    """

    def __init__(self, specs: List[Tuple[str, Tuple[int, int], pa.lib.DataType]]):
        self._spec_type = specs[0][0].title()
        self.splits = list(zip(*specs))[1]
        self.schema = pa.schema(
            [
                pa.field(name, datatype)
                for name, _, datatype in specs
            ]
        )

    def __str__(self):
        return f"{self._spec_type} File Specs"


form_specs = FileSpecs(
    [("form", (0, 12), pa.string()),
     ("company", (12, 74), pa.string()),
     ("cik", (74, 82), pa.int32()),
     ("filing_date", (85, 97), pa.string()),
     ("accession_number", (97, 141), pa.string())
     ]
)
company_specs = FileSpecs(
    [("company", (0, 62), pa.string()),
     ("form", (62, 74), pa.string()),
     ("cik", (74, 82), pa.int32()),
     ("filing_date", (85, 97), pa.string()),
     ("accession_number", (97, 141), pa.string())
     ]
)

FORM_INDEX_COLUMNS = ['form', 'company', 'cik', 'filing_date', 'accession_number']
COMPANY_INDEX_COLUMNS = ['company', 'form', 'cik', 'filing_date', 'accession_number']


def read_fixed_width_index(index_text: str,
                           file_specs: FileSpecs) -> pa.Table:
    """
    Read the index text as a fixed width file
    :param index_text: The index text as downloaded from SEC Edgar
    :param file_specs: The file specs containing the column definitions
    :return:
    """
    # Treat as a single array
    lines = index_text.rstrip('\n').split('\n')
    # Find where the data starts
    data_start = 0
    for index, line in enumerate(lines):
        if line.startswith("-----"):
            data_start = index + 1
            break
    data_lines = lines[data_start:]
    array = pa.array(data_lines)

    # Then split into separate arrays by file specs
    arrays = [
        pc.utf8_trim_whitespace(
            pc.utf8_slice_codeunits(array, start=start, stop=stop))
        for start, stop,
        in file_specs.splits
    ]

    # Change the CIK to int
    arrays[2] = pa.compute.cast(arrays[2], pa.int32())

    # Convert filingdate from string to date
    # Some files have %Y%m-%d other %Y%m%d
    date_format = '%Y-%m-%d' if len(arrays[3][0].as_py()) == 10 else '%Y%m%d'
    arrays[3] = pc.cast(pc.strptime(arrays[3], date_format, 'us'), pa.date32())

    # Get the accession number from the file directory_or_file
    arrays[4] = pa.compute.utf8_slice_codeunits(
        pa.compute.utf8_rtrim(arrays[4], characters=".txt"), start=-20)

    return pa.Table.from_arrays(
        arrays=arrays,
        names=list(file_specs.schema.names),
    )


def read_index_file(index_text: str, columns: List[str] = FORM_INDEX_COLUMNS) -> pa.Table:
    """
    Read the index text using multiple spaces as delimiter
    """
    # Split into lines and find the data start
    lines = index_text.rstrip('\n').split('\n')
    data_start = 0
    for index, line in enumerate(lines):
        if line.startswith("-----"):
            data_start = index + 1
            break

    # Process data lines
    data_lines = lines[data_start:]

    # Handle empty lines
    if not data_lines:
        return _empty_filing_index()

    # Split each line by 2 or more spaces
    rows = [line.split() for line in data_lines if line.strip()]

    # Convert to arrays
    forms = pa.array([line[:12].strip() for line in data_lines])  # The form might contain spaces like '1-A POS'

    # Company names may have single spaces within them
    companies = pa.array([' '.join(row[1:-3]) for row in rows])

    # CIKs are always the third-to-last field
    ciks = pa.array([int(row[-3]) for row in rows], type=pa.int32())

    # Dates are always second-to-last field
    dates = pc.strptime(pa.array([row[-2] for row in rows]), '%Y-%m-%d', 'us')
    dates = pc.cast(dates, pa.date32())

    # Accession numbers are in the file path
    accession_numbers = pa.array([row[-1][-24:-4] for row in rows])

    return pa.Table.from_arrays(
        [forms, companies, ciks, dates, accession_numbers],
        names=columns
    )


def read_form_index_file(index_text: str) -> pa.Table:
    """Read the form index file"""
    return read_index_file(index_text, columns=FORM_INDEX_COLUMNS)


def read_company_index_file(index_text: str) -> pa.Table:
    """Read the company index file"""
    return read_index_file(index_text, columns=COMPANY_INDEX_COLUMNS)


def read_pipe_delimited_index(index_text: str) -> pa.Table:
    """
    Read the index file as a pipe delimited index
    :param index_text: The index text as read from SEC Edgar
    :return: The index data as a pyarrow table
    """
    index_table = pa_csv.read_csv(
        BytesIO(index_text.encode()),
        parse_options=pa_csv.ParseOptions(delimiter="|"),
        read_options=pa_csv.ReadOptions(skip_rows=10,
                                        column_names=['cik', 'company', 'form', 'filing_date', 'accession_number'])
    )
    index_table = index_table.set_column(
        0,
        "cik",
        pa.compute.cast(index_table[0], pa.int32())
    ).set_column(4,
                 "accession_number",
                 pc.utf8_slice_codeunits(index_table[4], start=-24, stop=-4))
    return index_table


def fetch_filing_index(year_and_quarter: YearAndQuarter,
                       index: str
                       ):
    year, quarter = year_and_quarter
    url = full_index_url.format(year, quarter, index, "gz")
    try:
        index_table = fetch_filing_index_at_url(url, index)
        return (year, quarter), index_table
    except httpx.HTTPStatusError as e:
        if is_start_of_quarter() and e.response.status_code == 403:
            # Return an empty filing index
            return (year, quarter), _empty_filing_index()
        else:
            raise


def fetch_daily_filing_index(date: str,
                             index: str = 'form'):
    year, month, day = date.split("-")
    quarter = (int(month) - 1) // 3 + 1
    url = daily_index_url.format(year, quarter, index, date.replace("-", ""))
    index_table = fetch_filing_index_at_url(url, index)
    return index_table


def fetch_filing_index_at_url(url: str,
                              index: str) -> Optional[pa.Table]:
    index_text = download_text(url=url)
    assert index_text is not None
    if index == "xbrl":
        index_table: pa.Table = read_pipe_delimited_index(str(index_text))
    else:
        # Read as a fixed width index file
        columns = FORM_INDEX_COLUMNS if index == "form" else COMPANY_INDEX_COLUMNS
        index_table: pa.Table = read_index_file(index_text, columns=columns)
    return index_table


def _empty_filing_index():
    schema = pa.schema([
        ('form', pa.string()),
        ('company', pa.string()),
        ('cik', pa.int32()),
        ('filing_date', pa.date32()),
        ('accession_number', pa.string()),
    ])

    # Create an empty table with the defined schema
    return pa.Table.from_arrays([
        pa.array([], type=pa.string()),
        pa.array([], type=pa.string()),
        pa.array([], type=pa.int32()),
        pa.array([], type=pa.date32()),
        pa.array([], type=pa.string()),
    ], schema=schema)


def get_filings_for_quarters(year_and_quarters: YearAndQuarters,
                             index="form") -> pa.Table:
    """
    Get the filings for the quarters
    :param year_and_quarters:
    :param index: The index to use - "form", "company", or "xbrl"
    :return: The filings as a pyarrow table
    """

    if len(year_and_quarters) == 1:
        _, final_index_table = fetch_filing_index(year_and_quarter=year_and_quarters[0],
                                                  index=index)
    else:
        quarters_and_indexes = parallel(fetch_filing_index,
                                        items=year_and_quarters,
                                        index=index,
                                        threadpool=True,
                                        progress=True
                                        )
        quarter_and_indexes_sorted = sorted(quarters_and_indexes, key=lambda d: d[0])
        index_tables = [fd[1] for fd in quarter_and_indexes_sorted]
        final_index_table: pa.Table = pa.concat_tables(index_tables, mode="default")
    return final_index_table


class Filings:
    """
    A container for filings
    """

    def __init__(self,
                 filing_index: pa.Table,
                 original_state: Optional[PagingState] = None):
        self.data: pa.Table = filing_index
        self.data_pager = DataPager(self.data)
        # This keeps track of where the index should start in case this is just a page in the Filings
        self._original_state = original_state or PagingState(0, len(self.data))
        self._hash = None

    def to_pandas(self, *columns) -> pd.DataFrame:
        """Return the filing index as a python dataframe"""
        df = self.data.to_pandas()
        return df.filter(columns) if len(columns) > 0 else df

    def save_parquet(self, location: str):
        """Save the filing index as parquet"""
        pq.write_table(self.data, location)

    def save(self, location: str):
        """Save the filing index as parquet"""
        self.save_parquet(location)

    def get_filing_at(self, item: int):
        """Get the filing at the specified index"""
        return Filing(
            cik=self.data['cik'][item].as_py(),
            company=self.data['company'][item].as_py(),
            form=self.data['form'][item].as_py(),
            filing_date=self.data['filing_date'][item].as_py(),
            accession_no=self.data['accession_number'][item].as_py(),
        )

    @property
    def date_range(self) -> Tuple[datetime, datetime]:
        """Return a tuple of the start and end dates in the filing index"""
        min_max_dates: dict[str, datetime] = pc.min_max(self.data['filing_date']).as_py()
        return min_max_dates['min'], min_max_dates['max']

    @property
    def start_date(self) -> Optional[str]:
        """Return the start date for the filings"""
        return str(self.date_range[0]) if self.date_range[0] else self.date_range[0]

    @property
    def end_date(self) -> str:
        """Return the end date for the filings"""
        return str(self.date_range[1]) if self.date_range[1] else self.date_range[1]

    def latest(self, n: int = 1):
        """Get the latest n filings"""
        sort_indices = pc.sort_indices(self.data, sort_keys=[("filing_date", "descending")])
        sort_indices_top = sort_indices[:min(n, len(sort_indices))]
        latest_filing_index = pc.take(data=self.data, indices=sort_indices_top)
        filings = Filings(latest_filing_index)
        if len(filings) == 1:
            return filings[0]
        return filings

    def filter(self, *,
               form: Optional[Union[str, List[IntString]]] = None,
               amendments: bool = None,
               filing_date: Optional[str] = None,
               date: Optional[str] = None,
               cik: Union[IntString, List[IntString]] = None,
               exchange: Union[str, List[str], Exchange, List[Exchange]] = None,
               ticker: Union[str, List[str]] = None,
               accession_number: Union[str, List[str]] = None) -> Optional['Filings']:
        """
        Get some filings

        >>> filings = get_filings()

        Filter the filings

        On a date
        >>> filings.filter(date="2020-01-01")

        Up to a date
        >>> filings.filter(date=":2020-03-01")

        From a date
        >>> filings.filter(date="2020-01-01:")

        # Between dates
        >>> filings.filter(date="2020-01-01:2020-03-01")

        :param form: The form or list of forms to filter by
        :param amendments: Whether to include amendments to the forms e.g. include "10-K/A" if filtering for "10-K"
        :param filing_date: The filing date
        :param date: An alias for the filing date
        :param cik: The CIK or list of CIKs to filter by
        :param exchange: The exchange or list of exchanges to filter by
        :param ticker: The ticker or list of tickers to filter by
        :param accession_number: The accession number or list of accession numbers to filter by
        :return: The filtered filings
        """
        filing_index = self.data
        forms = form

        if isinstance(forms, list):
            forms = [str(f) for f in forms]

        # Filter by form
        if forms:
            filing_index = filter_by_form(filing_index, form=forms, amendments=amendments)
        elif amendments is not None:
            # Get the unique values of the form as a pylist
            forms = list(set([form.replace("/A", "") for form in pc.unique(filing_index['form']).to_pylist()]))
            filing_index = filter_by_form(filing_index, form=forms, amendments=amendments)

        # filing_date and date are aliases
        filing_date = filing_date or date
        if filing_date:
            try:
                filing_index = filter_by_date(filing_index, filing_date, 'filing_date')
            except InvalidDateException as e:
                log.error(e)
                return None

        # Filter by cik
        if cik:
            filing_index = filter_by_cik(filing_index, cik)

        # Filter by exchange
        if exchange:
            filing_index = filter_by_exchange(filing_index, exchange)

        if ticker:
            filing_index = filter_by_ticker(filing_index, ticker)

        # Filter by accession number
        if accession_number:
            filing_index = filter_by_accession_number(filing_index, accession_number=accession_number)

        return Filings(filing_index)

    def _head(self, n):
        assert n > 0, "The number of filings to select - `n`, should be greater than 0"
        return self.data.slice(0, min(n, len(self.data)))

    def head(self, n: int):
        """Get the first n filings"""
        selection = self._head(n)
        return Filings(selection)

    def _tail(self, n):
        assert n > 0, "The number of filings to select - `n`, should be greater than 0"
        return self.data.slice(max(0, len(self.data) - n), len(self.data))

    def tail(self, n: int):
        """Get the last n filings"""
        selection = self._tail(n)
        return Filings(selection)

    def _sample(self, n: int):
        assert len(self) >= n > 0, \
            "The number of filings to select - `n`, should be greater than 0 and less than the number of filings"
        return self.data.take(np.random.choice(len(self), n, replace=False)).sort_by([("filing_date", "descending")])

    def sample(self, n: int):
        """Get a random sample of n filings"""
        selection = self._sample(n)
        return Filings(selection)

    @property
    def empty(self) -> bool:
        return len(self.data) == 0

    def current(self):
        """Display the current page ... which is the default for this filings object"""
        return self

    def next(self):
        """Show the next page"""
        data_page = self.data_pager.next()
        if data_page is None:
            log.warning("End of data .. use prev() \u2190 ")
            return None
        start_index, _ = self.data_pager._current_range
        filings_state = PagingState(page_start=start_index, num_records=len(self))
        return Filings(data_page, original_state=filings_state)

    def previous(self):
        """
        Show the previous page of the data
        :return:
        """
        data_page = self.data_pager.previous()
        if data_page is None:
            log.warning(" No previous data .. use next() \u2192 ")
            return None
        start_index, _ = self.data_pager._current_range
        filings_state = PagingState(page_start=start_index, num_records=len(self))
        return Filings(data_page, original_state=filings_state)

    def prev(self):
        """Alias for self.previous()"""
        return self.previous()

    def _get_by_accession_number(self, accession_number: str):
        mask = pc.equal(self.data['accession_number'], accession_number)
        idx = mask.index(True).as_py()
        if idx > -1:
            return self.get_filing_at(idx)

    def get(self, index_or_accession_number: IntString):
        """
        First, get some filings
        >>> filings = get_filings()

        Get the Filing at that index location or that has the accession number
        >>> filings.get(100)

        >>> filings.get("0001721868-22-000010")

        :param index_or_accession_number:
        :return:
        """
        if isinstance(index_or_accession_number, int) or index_or_accession_number.isdigit():
            return self.get_filing_at(int(index_or_accession_number))
        else:
            accession_number = index_or_accession_number.strip()
            mask = pc.equal(self.data['accession_number'], accession_number)
            idx = mask.index(True).as_py()
            if idx > -1:
                return self.get_filing_at(idx)
            if not accession_number_re.match(accession_number):
                log.warning(
                    f"Invalid accession number [{accession_number}]"
                    "\n  valid accession number [0000000000-00-000000]"
                )

    def find(self,
             company_search_str: str):
        from edgar.entities import find_company

        # Search for the company
        search_results = find_company(company_search_str)

        return self.filter(cik=search_results.ciks)

    def to_dict(self, max_rows: int = 1000) -> Dict[str, Any]:
        """Return the filings as a json string but only the first max_rows records"""
        return cast(Dict[str, Any], self.to_pandas().head(max_rows).to_dict(orient="records"))

    def __getitem__(self, item):
        return self.get_filing_at(item)

    def __len__(self):
        return len(self.data)

    def __iter__(self):
        self.n = 0
        return self

    def __next__(self):
        if self.n < len(self.data):
            filing: Filing = self[self.n]
            self.n += 1
            return filing
        else:
            raise StopIteration

    @property
    def summary(self):
        return (f"Showing {self.data_pager.page_size} of "
                f"{self._original_state.num_records:,} filings")

    def _page_index(self) -> range:
        """Create the range index to set on the page dataframe depending on where in the data we are
        """
        if self._original_state:
            return range(self._original_state.page_start,
                         self._original_state.page_start
                         + min(self.data_pager.page_size, len(self.data)))  # set the index to the size of the page
        else:
            return range(*self.data_pager._current_range)

    def __eq__(self, other):
        # Check if other is Filings or subclass of Filings
        if not isinstance(other, self.__class__) and not issubclass(other.__class__, self.__class__):
            return False

        if len(self) != len(other):
            return False

        if self.start_date != other.start_date or self.end_date != other.end_date:
            return False

        # Handle empty tables
        if len(self) == 0:
            return True  # Two empty tables with same dates are equal

        # Compare just accession_number columns
        return self.data['accession_number'].equals(other.data['accession_number'])


    def __hash__(self):
        if self._hash is None:
            # Base hash components
            hash_components = [self.__class__.__name__, len(self), self.start_date, self.end_date]

            # Only add accession numbers if table is not empty
            if len(self) > 0:
                # Handle different table sizes appropriately
                if len(self) == 1:
                    hash_components.append(self.data['accession_number'][0].as_py())
                elif len(self) == 2:
                    hash_components.append(self.data['accession_number'][0].as_py())
                    hash_components.append(self.data['accession_number'][1].as_py())
                else:
                    hash_components.append(self.data['accession_number'][0].as_py())
                    hash_components.append(self.data['accession_number'][len(self) // 2].as_py())
                    hash_components.append(self.data['accession_number'][len(self) - 1].as_py())

            self._hash = hash(tuple(hash_components))
        return self._hash

    def __rich__(self) -> Panel:
        # Create table with appropriate columns and styling
        table = Table(
            show_header=True,
            header_style="bold",
            show_edge=True,
            expand=False,
            padding=(0, 1),
            box=box.SIMPLE,
            row_styles=["", "bold"]
        )

        # Add columns with specific styling and alignment
        table.add_column("#", style="dim", justify="right")
        table.add_column("Form", width=7)
        table.add_column("CIK", style="dim", width=10, justify="right")
        table.add_column("Ticker", width=6, style="yellow")
        table.add_column("Company", style="bold green", width=38, no_wrap=True)
        table.add_column("Filing Date", width=11)
        table.add_column("Accession Number", style="dim", width=20)

        # Get current page from data pager
        current_page = self.data_pager.current()

        # Calculate start index for proper indexing
        start_idx = self._original_state.page_start if self._original_state else self.data_pager.start_index

        # Iterate through rows in current page
        for i in range(len(current_page)):
            cik = current_page['cik'][i].as_py()
            ticker = find_ticker(cik)

            row = [
                str(start_idx + i),
                current_page['form'][i].as_py(),
                str(cik),
                ticker,
                current_page['company'][i].as_py(),
                str(current_page['filing_date'][i].as_py()),
                current_page['accession_number'][i].as_py()
            ]
            table.add_row(*row)

        # Show paging information only if there are multiple pages
        elements = [table]

        if self.data_pager.total_pages > 1:
            total_filings = self._original_state.num_records
            current_count = len(current_page)
            start_num = start_idx + 1
            end_num = start_idx + current_count

            page_info = Text.assemble(
                ("Showing ", "dim"),
                (f"{start_num:,}", "bold red"),
                (" to ", "dim"),
                (f"{end_num:,}", "bold red"),
                (" of ", "dim"),
                (f"{total_filings:,}", "bold"),
                (" filings.", "dim"),
                (" Page using ", "dim"),
                ("← prev()", "bold gray54"),
                (" and ", "dim"),
                ("next() →", "bold gray54")
            )

            elements.extend([Text("\n"), page_info])

        # Get the subtitle
        start_date, end_date = self.date_range
        subtitle = f"SEC Filings between {start_date:%Y-%m-%d} and {end_date:%Y-%m-%d}" if start_date else ""
        return Panel(
            Group(*elements),
            title="SEC Filings",
            subtitle=subtitle,
            border_style="bold grey54",
            expand=False
        )

    def __repr__(self):
        return repr_rich(self.__rich__())


def sort_filings_by_priority(filing_table: pa.Table,
                             priority_forms: Optional[List[str]] = None) -> pa.Table:
    """
    Sort a filings table by date (descending) and form priority.

    Args:
        filing_table: PyArrow table containing filings data
        priority_forms: List of forms in priority order. Forms not in list will be sorted
                       alphabetically after priority forms. Defaults to common forms if None.

    Returns:
        PyArrow table sorted by date and form priority
    """
    if priority_forms is None:
        priority_forms = ['10-Q', '10-Q/A', '10-K', '10-K/A', '8-K', '8-K/A',
                          '6-K', '6-K/A', '13F-HR', '144', '4', 'D', 'SC 13D', 'SC 13G']

    # Create form priority values
    forms_array = filing_table['form']
    priorities = []
    for form_type in forms_array.to_pylist():
        try:
            priority = priority_forms.index(form_type)
        except ValueError:
            priority = len(priority_forms)
        priorities.append(priority)

    # Add priority column
    with_priority = filing_table.append_column(
        'form_priority',
        pa.array(priorities, type=pa.int32())
    )

    # Sort by date (descending), priority (ascending), form name (ascending)
    sorted_table = with_priority.sort_by([
        ("filing_date", "descending"),
        ("form_priority", "ascending"),
        ("form", "ascending")
    ])

    # Remove temporary priority column
    return sorted_table.drop(['form_priority'])


def get_filings(year: Optional[Years] = None,
                quarter: Optional[Quarters] = None,
                form: Optional[Union[str, List[IntString]]] = None,
                amendments: bool = True,
                filing_date: Optional[str] = None,
                index="form",
                priority_forms: Optional[List[str]] = None) -> Optional[Filings]:
    """
    Downloads the filing index for a given year or list of years, and a quarter or list of quarters.

    So you can download for 2020, [2020,2021,2022] or range(2020, 2023)

    Examples

    >>> from edgar import get_filings

    >>> filings_ = get_filings(2021) # Get filings for 2021

    >>> filings_ = get_filings(2021, 4) # Get filings for 2021 Q4

    >>> filings_ = get_filings(2021, [3,4]) # Get filings for 2021 Q3 and Q4

    >>> filings_ = get_filings([2020, 2021]) # Get filings for 2020 and 2021

    >>> filings_ = get_filings([2020, 2021], 4) # Get filings for Q4 of 2020 and 2021

    >>> filings_ = get_filings(range(2010, 2021)) # Get filings between 2010 and 2021 - does not include 2021

    >>> filings_ = get_filings(2021, 4, form="D") # Get filings for 2021 Q4 for form D

    >>> filings_ = get_filings(2021, 4, filing_date="2021-10-01") # Get filings for 2021 Q4 on "2021-10-01"

    >>> filings_ = get_filings(2021, 4, filing_date="2021-10-01:2021-10-10") # Get filings for 2021 Q4 between
                                                                            # "2021-10-01" and "2021-10-10"


    :param year The year of the filing
    :param quarter The quarter of the filing
    :param form The form or forms as a string e.g. "10-K" or a List ["10-K", "8-K"]
    :param amendments If True will expand the list of forms to include amendments e.g. "10-K/A"
    :param filing_date The filing date to filter by in YYYY-MM-DD format
                e.g. filing_date="2022-01-17" or filing_date="2022-01-17:2022-02-28"
    :param index The index type - "form" or "company" or "xbrl"
    :return:
    """
    # Get the year or default to the current year
    using_default_year = False
    if filing_date:
        if not is_valid_filing_date(filing_date):
            log.warning("""Provide a valid filing date in the format YYYY-MM-DD or YYYY-MM-DD:YYYY-MM-DD""")
            return None
        year_and_quarters = filing_date_to_year_quarters(filing_date)
    elif not year:
        # If no year specified, take the current year
        year, _ = current_year_and_quarter()
        year_and_quarters: YearAndQuarters = expand_quarters(year, quarter)
        using_default_year = True
    else:
        year_and_quarters: YearAndQuarters = expand_quarters(year, quarter)

    if len(year_and_quarters) == 0:
        log.warning(f"""
    Provide a year between 1994 and {datetime.now().year} and optionally a quarter (1-4) for which the SEC has filings. 
    
        e.g. filings = get_filings(2023) OR
             filings = get_filings(2023, 1)
    
    (You specified the year {year} and quarter {quarter})   
        """)
        return None
    filing_index = get_filings_for_quarters(year_and_quarters, index=index)

    filings = Filings(filing_index)

    if form or filing_date:
        filings = filings.filter(form=form, amendments=amendments, filing_date=filing_date)

    if not filings:
        if using_default_year:
            # Ensure at least some data is returned
            previous_quarter = [get_previous_quarter(year, quarter)]
            filing_index = get_filings_for_quarters(previous_quarter, index=index)
            filings = Filings(filing_index)
            sorted_filing_index = sort_filings_by_priority(filings.data, priority_forms)
            return Filings(sorted_filing_index)
        return None

    # Sort the filings using the separate sort function
    sorted_filing_index = sort_filings_by_priority(filings.data, priority_forms)

    return Filings(sorted_filing_index)


"""
Get the current filings from the SEC. Use this to get the filings filed after the 5:30 deadline
"""
GET_CURRENT_URL = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&output=atom&owner=only&count=100"
title_regex = re.compile(r"(.*) - (.*) \((\d+)\) \((.*)\)")
summary_regex = re.compile(r'<b>([^<]+):</b>\s+([^<\s]+)')


def parse_title(title: str):
    """
    Given the title in this example

    "144 - monday.com Ltd. (0001845338) (Subject)"
    which contains the form type, company name, CIK, and status
    parse into a tuple of form type, company name, CIK, and status using regex
    """
    match = title_regex.match(title)
    assert match, f"Could not parse title: {title} using regex: {title_regex}"
    return match.groups()


def parse_summary(summary: str):
    """
    Given the summary in this example

    "Filed: 2021-09-30 AccNo: 0001845338-21-000002 Size: 1 MB"

    parse into a tuple of filing date, accession number, and size
    """
    # Remove <b> and </b> tags from summary

    matches = re.findall(summary_regex, summary)

    # Convert matches into a dictionary
    fields = {k.strip(): (int(v) if v.isdigit() else v) for k, v in matches}

    return datetime.strptime(str(fields.get('Filed', '')), '%Y-%m-%d').date(), fields.get('AccNo')


def get_current_url(atom: bool = True,
                    count: int = 100,
                    start: int = 0,
                    form: str = '',
                    owner: str = 'include'):
    url = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"

    count = count if count in [10, 20, 40, 80, 100] else 40
    owner = owner if owner in ['include', 'exclude', 'only'] else 'include'

    url = url + f"&count={count}&start={start}&type={form}&owner={owner}"
    if atom:
        url += "&output=atom"
    return url


def get_current_entries_on_page(count: int, start: int, form: Optional[str] = None, owner: str = 'include'):
    url = get_current_url(count=count, start=start, form=form if form else '', owner=owner, atom=True)
    response = get_with_retry(url)

    soup = BeautifulSoup(response.text, features="xml")
    entries = []
    for entry in soup.find_all("entry"):
        # The title contains the form type, company name, CIK, and status e.g 4 - WILKS LEWIS (0001076463) (Reporting)
        title = child_text(entry, "title")
        form_type, company_name, cik, status = parse_title(title)
        # The summary contains the filing date and link to the filing
        summary = child_text(entry, "summary")
        filing_date, accession_number = parse_summary(summary)

        entries.append({'form': form_type,
                        'company': company_name,
                        'cik': cik,
                        'filing_date': filing_date,
                        'accession_number': accession_number})
    return entries


def get_current_filings(form: str = '',
                        owner: str = 'include',
                        page_size: int = 40):
    """
    Get the current filings from the SEC
    :return: The current filings from the SEC
    """
    owner = owner if owner in ['include', 'exclude', 'only'] else 'include'
    page_size = page_size if page_size in [10, 20, 40, 80, 100] else 40
    start = 0

    entries = get_current_entries_on_page(count=page_size, start=start, form=form, owner=owner)
    if not entries:
        return CurrentFilings(filing_index=_empty_filing_index(), owner=owner, form=form, page_size=page_size)
    return CurrentFilings(filing_index=pa.Table.from_pylist(entries), owner=owner, form=form, page_size=page_size)


class CurrentFilings(Filings):
    """
    This version of the Filings class is used to get the current filings from the SEC
    page by page
    """

    def __init__(self,
                 filing_index: pa.Table,
                 form: str = '',
                 start: int = 1,
                 page_size: int = 40,
                 owner: str = 'include'):
        super().__init__(filing_index, original_state=None)
        self._start = start
        self._page_size = page_size
        self.owner = owner
        self.form = form

    def next(self):
        # If the number of entries is less than the page size then we are at the end of the data
        if len(self.data) < self._page_size:
            return None
        start = self._start + len(self.data)
        next_entries = get_current_entries_on_page(start=start, count=self._page_size, form=self.form)
        if next_entries:
            # Copy the values to this Filings object and return it
            self.data = pa.Table.from_pylist(next_entries)
            self._start = start
            return self

    def previous(self):
        # If start = 1 then there are no previous entries
        if self._start == 1:
            return None
        start = max(1, self._start - self._page_size)
        previous_entries = get_current_entries_on_page(start=start, count=self._page_size, form=self.form)
        if previous_entries:
            # Copy the values to this Filings object and return it
            self.data = pa.Table.from_pylist(previous_entries)
            self._start = start
            return self

    def __getitem__(self, item):  # type: ignore
        item = self.get(item)
        assert item is not None
        return item

    def get(self, index_or_accession_number: IntString):
        if isinstance(index_or_accession_number, int) or index_or_accession_number.isdigit():
            idx = int(index_or_accession_number)
            if self._start - 1 <= idx < self._start - 1 + len(self.data):
                # Where on this page is the index
                idx_on_page = idx - (self._start - 1)
                return super().get_filing_at(idx_on_page)
        else:
            accession_number = index_or_accession_number.strip()
            # See if the filing is in this page
            filing = super().get(accession_number)
            if filing:
                return filing

            current_filings = get_current_filings(self.form, self.owner, page_size=100)
            filing = CurrentFilings._get_current_filing_by_accession_number(current_filings.data, accession_number)
            if filing:
                return filing
            with Status(f"[bold deep_sky_blue1]Searching through the most recent filings for {accession_number}...",
                        spinner="dots2"):
                while True:
                    current_filings = current_filings.next()
                    if current_filings is None:
                        return None
                    filing = CurrentFilings._get_current_filing_by_accession_number(current_filings.data,
                                                                                    accession_number)
                    if filing:
                        return filing

    @staticmethod
    def _get_current_filing_by_accession_number(data: pa.Table, accession_number: str):
        mask = pc.equal(data['accession_number'], accession_number)
        idx = mask.index(True).as_py()
        if idx > -1:
            return Filing(
                cik=data['cik'][idx].as_py(),
                company=data['company'][idx].as_py(),
                form=data['form'][idx].as_py(),
                filing_date=data['filing_date'][idx].as_py(),
                accession_no=data['accession_number'][idx].as_py(),
            )
        return None

    def __rich__(self):

        # Create table with appropriate columns and styling
        table = Table(
            show_header=True,
            header_style="bold",
            show_edge=True,
            expand=False,
            padding=(0, 1),
            box=box.SIMPLE,
            row_styles=["", "bold"]
        )

        # Add columns with specific styling and alignment
        table.add_column("#", style="dim", justify="right")
        table.add_column("Form", width=7)
        table.add_column("CIK", style="dim", width=10, justify="right")
        table.add_column("Ticker", width=6, style="yellow")
        table.add_column("Company", style="bold green", width=38, no_wrap=True)
        table.add_column("Filing Date", width=11)
        table.add_column("Accession Number", style="dim", width=20)

        # Get current page from data pager
        current_page = self.data.to_pandas()

        # compute the index from the start and page_size and set it as the index of the page
        current_page.index = range(self._start - 1, self._start - 1 + len(current_page))

        # Iterate through rows in current page
        for t in current_page.itertuples():
            cik = t.cik
            ticker = find_ticker(cik)

            row = [
                str(t.Index),
                t.form,
                str(cik),
                ticker,
                t.company,
                str(t.filing_date),
                t.accession_number
            ]
            table.add_row(*row)

        # Show paging information only if there are multiple pages
        elements = [table]

        page_info = Text.assemble(
            ("Showing ", "dim"),
            (f"{current_page.index.min():,}", "bold red"),
            (" to ", "dim"),
            (f"{current_page.index.max():,}", "bold red"),
            (" most recent filings.", "dim"),
            (" Page using ", "dim"),
            ("← prev()", "bold gray54"),
            (" and ", "dim"),
            ("next() →", "bold gray54")
        )

        elements.extend([Text("\n"), page_info])

        # Get the subtitle
        start_date, end_date = self.date_range
        subtitle = "Most recent filings from the SEC"
        return Panel(
            Group(*elements),
            title="SEC Filings",
            subtitle=subtitle,
            border_style="bold grey54"
        )


def _get_cached_filings(year: Optional[Years] = None,
                        quarter: Optional[Quarters] = None,
                        form: Optional[Union[str, List[IntString]]] = None,
                        amendments: bool = True,
                        filing_date: Optional[str] = None,
                        index="form") -> Union[Filings, None]:
    # Get the filings but cache the result
    return get_filings(year=year, quarter=quarter, form=form, amendments=amendments, filing_date=filing_date,
                       index=index)


def parse_filing_header(content):
    data = {}
    current_key = None

    lines = content.split('\n')
    for line in lines:
        if line.endswith(':'):
            current_key = line[:-1]  # Remove the trailing colon
            data[current_key] = {}
        elif current_key and ':' in line:
            key, value = map(str.strip, line.split(':', 1))
            data[current_key][key] = value

    return data


def _create_address_table(business_address: Address, mailing_address: Address):
    address_table = Table("Type", "Street1", "Street2", "City", "State", "Zipcode",
                          title="\U0001F4EC Addresses", box=box.SIMPLE)
    if business_address:
        address_table.add_row("\U0001F3E2 Business Address",
                              business_address.street1,
                              business_address.street2,
                              business_address.city,
                              business_address.state_or_country,
                              business_address.zipcode)

    if mailing_address:
        address_table.add_row("\U0001F4ED Mailing Address",
                              mailing_address.street1,
                              mailing_address.street2,
                              mailing_address.city,
                              mailing_address.state_or_country,
                              mailing_address.zipcode)
    return address_table


class Filing:
    """
    A single SEC filing. Allow you to access the documents and data for that filing
    """

    def __init__(self,
                 cik: int,
                 company: str,
                 form: str,
                 filing_date: str,
                 accession_no: str):
        self.cik = cik
        self.company = company
        self.form = form
        self.filing_date = filing_date
        self.accession_no = accession_no
        self._filing_homepage = None

    @property
    def accession_number(self):
        return self.accession_no

    @property
    def document(self):
        """
        :return: The primary display document on the filing, generally HTML but can be XHTML
        """
        document = self.sgml().attachments.primary_html_document
        # If the document is not in the SGML then we have to go to the homepage
        if document:
            return document
        return self.homepage.primary_html_document

    @property
    def primary_documents(self):
        """
        :return: a list of the primary documents on the filing, generally HTML or XHTML and optionally XML
        """
        documents = self.sgml().attachments.primary_documents
        if len(documents) == 0:
            documents = self.homepage.primary_documents
        return documents

    @property
    def period_of_report(self):
        """
        Get the period of report for the filing
        """
        return self.homepage.period_of_report

    @property
    def attachments(self):
        # Return all the attachments on the filing
        sgml_filing: FilingSGML = self.sgml()
        return sgml_filing.attachments

    @property
    def exhibits(self):
        # Return all the exhibits on the filing
        return self.attachments.exhibits

    def html(self) -> Optional[str]:
        """Returns the html contents of the primary document if it is html"""
        sgml = self.sgml()
        html = sgml.html()
        if html and not html.startswith("<?xml"):
            # skip PDF (for now)
            if html.endswith("</PDF>"):
                return None
            if has_html_content(html):
                return html
            return None
        # If the html document is not in the SGML then we have to go to the homepage
        html = self.homepage.primary_html_document.download()
        if isinstance(html, bytes):
            try:
                return html.decode("utf-8")
            except UnicodeDecodeError:
                return None
        return html

    def xml(self) -> Optional[str]:
        """Returns the xml contents of the primary document if it is xml"""
        sgml = self.sgml()
        return sgml.xml()

    def text(self) -> str:
        """Convert the html of the main filing document to text"""
        html_content = self.html()
        if html_content and has_html_content(html_content):
            document = Document.parse(html_content)
            return rich_to_text(document)
        else:
            text_extract_attachments = self.attachments.query("document_type == 'TEXT-EXTRACT'")
            if len(text_extract_attachments) > 0 and text_extract_attachments.get_by_index(0) is not None:
                text_extract_attachment = text_extract_attachments.get_by_index(0)
                return text_extract_attachment.content
            else:
                return self._download_filing_text()

    def _download_filing_text(self):
        """
        Download the text of the filing directly from the primary text sources.
        Either from the text url or the text extract attachment
        """
        text_extract_attachments = self.attachments.query("document_type == 'TEXT-EXTRACT'")
        if len(text_extract_attachments) > 0 and text_extract_attachments[0] is not None:
            text_extract_attachment = text_extract_attachments[0]
            assert text_extract_attachment is not None
            return download_text_between_tags(text_extract_attachment.url, "TEXT")
        else:
            return download_text_between_tags(self.text_url, "TEXT")

    def full_text_submission(self) -> str:
        """Return the complete text submission file"""
        downloaded = download_file(self.text_url, as_text=True)
        assert downloaded is not None
        return str(downloaded)

    def markdown(self) -> str:
        """return the markdown version of this filing html"""
        html = self.html()
        if html:
            clean_html = get_clean_html(html)
            if clean_html:
                return to_markdown(clean_html)
        text_content = self.text()
        return text_to_markdown(text_content)

    def view(self):
        """Preview this filing's primary document as markdown. This should display in the console"""
        html_content = self.html()
        if html_content:
            document = Document.parse(html_content)
            print_rich(document)
        else:
            print(self.text())

    def xbrl(self) -> Optional[Union[XBRLData, XBRLInstance]]:
        """
        Get the XBRL document for the filing, parsed and as a FilingXbrl object
        :return: Get the XBRL document for the filing, parsed and as a FilingXbrl object, or None
        """
        return get_xbrl_object(self)

    def serve(self, port: int = 8000) -> AttachmentServer:
        """Serve the filings on a local server
        port: The port to serve the filings on
        """
        return self.attachments.serve(port=port)

    def save(self, directory_or_file: PathLike):
        """Save the filing to a directory path or a file using pickle.dump

            If directory_or_file is a directory then the final file will be

            '<directory>/<accession_number>.pkl'

            Otherwise, save to the file passed in
        """
        filing_path = Path(directory_or_file)
        if filing_path.is_dir():
            filing_path = filing_path / f"{self.accession_no}.pkl"
        with filing_path.open("wb") as f:
            pickle.dump(self, f)

    @classmethod
    def load(cls, path: PathLike):
        """Load a filing from a json file"""
        path = Path(path)
        with path.open("rb") as file:
            return pickle.load(file)

    @cached_property
    def filing_directory(self) -> FilingDirectory:
        return FilingDirectory.load(self.base_dir)

    def _local_path(self) -> Path:
        """
        Get the local path for the filing
        """
        return local_filing_path(str(self.filing_date), self.accession_no)

    def sgml(self) -> FilingSGML:
        """
        Read the filing from the local storage path if it exists
        """
        if is_using_local_storage():
            local_path = local_filing_path(str(self.filing_date), self.accession_no)
            if local_path.exists():
                return FilingSGML.from_source(local_path)
        return FilingSGML.from_filing(self)

    @cached_property
    def reports(self)  -> Optional[Reports]:
        """
        If the filing has report attachments then return the reports
        """
        filing_summary = self.sgml().filing_summary
        if filing_summary:
            return filing_summary.reports

    @cached_property
    def statements(self) -> Optional[Statements]:
        """
        Get the statements for a report
        """
        if self.reports:
            return self.reports.statements

    @cached_property
    def index_headers(self) -> IndexHeaders:
        """
        Get the index headers for the filing. This is a listing of all the files in the filing directory
        """
        index_headers_url = f"{self.base_dir}/{self.accession_no}-index-headers.html"
        index_header_text = download_text(index_headers_url)
        return IndexHeaders.load(index_header_text)

    def to_dict(self) -> Dict[str, Union[str, int]]:
        """Return the filing as a Dict string"""
        return {'accession_number': self.accession_number,
                'cik': self.cik,
                'company': self.company,
                'form': self.form,
                'filing_date': self.filing_date}

    @classmethod
    def from_dict(cls, data: Dict[str, Union[str, int]]):
        """Create a Filing from a dictionary.
        Thw dict must have the keys cik, company, form, filing_date, accession_no
        """
        assert all(key in data for key in ['cik', 'company', 'form', 'filing_date', 'accession_number']), \
            "The dict must have the keys cik, company, form, filing_date, accession_number"
        return cls(cik=int(data['cik']),
                   company=str(data['company']),
                   form=str(data['form']),
                   filing_date=str(data['filing_date']),
                   accession_no=str(data['accession_number']))

    @classmethod
    def from_json(cls, path: str):
        """Create a Filing from a JSON file"""
        with open(path, 'r') as file:
            data = json.load(file)
            return cls.from_dict(data)

    @property
    def header(self):
        _sgml = self.sgml()
        return _sgml.header


    def data_object(self):
        """ Get this filing as the data object that it might be"""
        from edgar import obj
        return obj(self)

    def obj(self):
        """Alias for data_object()"""
        return self.data_object()

    def open_homepage(self):
        """Open the homepage in the browser"""
        webbrowser.open(self.homepage_url)

    def open(self):
        """Open the main filing document"""
        assert self.document is not None
        # Use the homepage to determine the url since SGML sometimes miss the primary HTML file
        webbrowser.open(self.homepage.primary_html_document.url)

    def sections(self) -> List[str]:
        html = self.html()
        assert html is not None
        return html_sections(html)

    def __get_bm25_search_index(self):
        return BM25Search(self.sections())

    def __get_regex_search_index(self):
        return RegexSearch(self.sections())

    def search(self,
               query: str,
               regex=False):
        """Search for the query string in the filing HTML"""
        if regex:
            return self.__get_regex_search_index().search(query)
        return self.__get_bm25_search_index().search(query)

    @property
    def filing_url(self) -> str:
        return f"{self.base_dir}/{self.document.document}"

    @property
    def homepage_url(self) -> str:
        return f"{sec_edgar}/data/{self.cik}/{self.accession_no}-index.html"

    @property
    def text_url(self) -> str:
        return f"{self.base_dir}/{self.accession_no}.txt"

    @property
    def index_header_url(self) -> str:
        return f"{self.base_dir}/index-headers.html"

    @property
    def base_dir(self) -> str:
        return f"{sec_edgar}/data/{self.cik}/{self.accession_no.replace('-', '')}"

    @property
    def url(self) -> str:
        return self.homepage_url

    @property
    def homepage(self):
        """
        Get the homepage for the filing
        :return: the FilingHomepage
        """
        if not self._filing_homepage:
            self._filing_homepage = FilingHomepage.load(self.homepage_url)
        return self._filing_homepage

    @property
    def home(self):
        """Alias for homepage"""
        return self.homepage

    def get_entity(self):
        """Get the company to which this filing belongs"""
        "Get the company for cik. Cache for performance"
        from edgar.entities import CompanyData
        return CompanyData.for_cik(self.cik)

    def as_company_filing(self):
        """Get this filing as a company filing. Company Filings have more information"""
        company = self.get_entity()
        if not company:
            return None

        filings = company.get_filings(accession_number=self.accession_no)
        if filings and not filings.empty:
            return filings[0]

    def related_filings(self):
        """Get all the filings related to this one
        There is no file number on this base Filing class so first get the company,

        then this filing then get the related filings
        """
        company = self.get_entity()
        if not company:
            return

        filings = company.get_filings(accession_number=self.accession_no)
        if not filings or filings.empty:
            if is_using_local_storage():
                # In this case the local storage is missing the filing so we have to download it
                log.warning(f"Filing {self.accession_no} not found in local storage. Downloading from SEC ...")
                from edgar.entities import download_entity_submissions_from_sec, parse_entity_submissions
                submissions_json = download_entity_submissions_from_sec(self.cik)
                c_from_sec = parse_entity_submissions(submissions_json)
                filings = c_from_sec.get_filings(accession_number=self.accession_no)

                if not filings or filings.empty:
                    # Shouldn't get here
                    return company.get_empty_filings()
            else:
                return company.get_empty_filings()
        file_number = filings[0].file_number
        return company.get_filings(file_number=file_number,
                                   sort_by=[("filing_date", "ascending"), ("accession_number", "ascending")])

    def __hash__(self):
        return hash(self.accession_no)

    def __eq__(self, other):
        return isinstance(other, Filing) and self.accession_no == other.accession_no

    def __ne__(self, other):
        return not self == other

    def summary(self) -> pd.DataFrame:
        """Return a summary of this filing as a dataframe"""
        return pd.DataFrame([{"Accession Number": self.accession_no,
                              "Filing Date": self.filing_date,
                              "Company": self.company,
                              "CIK": self.cik}]).set_index("Accession Number")

    def __str__(self):
        """
        Return a string version of this filing e.g.

        Filing(form='10-K', filing_date='2018-03-08', company='CARBO CERAMICS INC',
              cik=1009672, accession_no='0001564590-18-004771')
        :return:
        """
        return (f"Filing(form='{self.form}', filing_date='{self.filing_date}', company='{self.company}', "
                f"cik={self.cik}, accession_no='{self.accession_no}')")

    def __rich__(self):
        """
        Produce a table version of this filing e.g.
        ┌──────────────────────┬──────┬────────────┬────────────────────┬─────────┐
        │                      │ form │ filing_date│ company            │ cik     │
        ├──────────────────────┼──────┼────────────┼────────────────────┼─────────┤
        │ 0001564590-18-004771 │ 10-K │ 2018-03-08 │ CARBO CERAMICS INC │ 1009672 │
        └──────────────────────┴──────┴────────────┴────────────────────┴─────────┘
        :return: a rich table version of this filing
        """
        ticker = find_ticker(self.cik)
        ticker = f"{ticker}" if ticker else ""

        # The title of the panel
        title = Text.assemble((f"Form {self.form} ", "bold"),
                              (self.company, "bold green"),
                              " ",
                              (f"[{self.cik}] ", "dim"),
                              (ticker, "bold yellow")
                              )
        # The subtitle of the panel
        subtitle = Text(describe_form(self.form, False), "dim")

        attachments = self.attachments
        # The filing information table
        filing_info_table = Table("Accession Number", "Filing Date", "Period of Report", "Documents",
                                  header_style="dim",
                                  box=box.SIMPLE_HEAD)
        filing_info_table.add_row(Text(self.accession_no, "bold deep_sky_blue1"),
                                  Text(str(self.filing_date), "bold"),
                                  Text(self.period_of_report or "-", "bold"),
                                  f"{len(attachments)}")
        return Panel(
            Group(filing_info_table),
            title=title,
            subtitle=subtitle,
            box=box.ROUNDED,
            height=10,
            expand=False
        )

    def __repr__(self):
        return repr_rich(self.__rich__())


# These are the columns on the table on the filing homepage
filing_file_cols = ['Seq', 'Description', 'Document', 'Type', 'Size', 'Url']


@dataclass(frozen=True)
class ClassContractSeries:
    cik: str
    url: str


@dataclass(frozen=True)
class ClassContract:
    cik: str
    name: str
    ticker: str
    status: str


@dataclass(frozen=True)
class FilerInfo:
    company_name: str
    identification: str
    addresses: List[str]

    def __rich__(self):
        return Panel(
            Columns([self.identification, Text("   "), self.addresses[0], self.addresses[1]]),
            title=self.company_name
        )

    def __repr__(self):
        return repr_rich(self.__rich__())


def summarize_files(data: pd.DataFrame) -> pd.DataFrame:
    return (data
            .filter(["Seq", "Document", "Description", "Size"])
            .assign(Size=data.Size.apply(display_size))
            .set_index("Seq")
            )


def get_filing_by_accession(accession_number: str, year: int):
    """Cache-friendly version that takes year as parameter instead of using datetime.now()"""
    assert re.match(r"\d{10}-\d{2}-\d{6}", accession_number)

    # Static logic that doesn't depend on current time
    for quarter in range(1, 5):
        filings = _get_cached_filings(year=year, quarter=quarter)
        if filings and (filing := filings.get(accession_number)):
            return filing

    return None


def get_by_accession_number(accession_number: str, show_progress: bool = False):
    """Wrapper that handles progress display and current time logic"""
    year = int("19" + accession_number[11:13]) if accession_number[11] == '9' else int("20" + accession_number[11:13])

    with Status("[bold deep_sky_blue1]Searching...", spinner="dots2") if show_progress else nullcontext():
        filing = get_filing_by_accession(accession_number, year)

        if not filing and year == datetime.now().year:
            filings = get_current_filings()
            filing = filings.get(accession_number)

    return filing


def form_with_amendments(*forms: str):
    return list(forms) + [f"{f}/A" for f in forms]


barchart = '\U0001F4CA'
ticket = '\U0001F3AB'
page_facing_up = '\U0001F4C4'
classical_building = '\U0001F3DB'


def unicode_for_form(form: str) -> str:
    """
    Returns a meaningful Unicode symbol based on SEC form type.

    Args:
        form (str): SEC form type identifier

    Returns:
        str: Unicode symbol representing the form type

    Form type categories:
    - Periodic Reports (10-K, 10-Q): 📊 (financial statements/data)
    - Current Reports (8-K, 6-K): ⚡ (immediate/material events)
    - Registration & Offerings:
        - S-1, F-1: 🎯 (initial public offerings)
        - S-3, F-3: 🔄 (follow-on offerings)
        - Prospectuses (424B*): 📖 (offering documents)
    - Insider Forms (3, 4, 5): 👥 (insider activity)
    - Beneficial Ownership:
        - SC 13D/G: 🏰 (significant ownership stakes)
        - 13F-HR: 📈 (institutional holdings)
    - Investment Company:
        - N-CSR, N-Q: 💼 (investment portfolio reports)
        - N-PX: 🗳️ (proxy voting record)
    - Foreign Company Forms (20-F, 40-F): 🌐 (international)
    - Municipal Advisor Forms (MA): ⚖️ (regulation/compliance)
    - Communications (CORRESP/UPLOAD): 💬 (dialogue with SEC)
    - Proxy Materials (DEF 14A): 📩 (shareholder voting)
    - Default: 📄 (generic document)
    """

    # Periodic financial reports
    if form in ['10-K', '10-Q', '10-K/A', '10-Q/A']:
        return '📊'  # Chart for financial statements

    # Current reports (material events)
    elif form in ['8-K', '8-K/A', '6-K', '6-K/A']:
        return '⚡'  # Lightning bolt for immediate/current events

    # Initial registration statements
    elif form.startswith(('S-1', 'F-1')) or form in ['S-1/A', 'F-1/A']:
        return '🎯'  # Target for initial offerings

    # Shelf registration statements
    elif form.startswith(('S-3', 'F-3')) or form in ['S-3/A', 'F-3/A']:
        return '🔄'  # Circular arrows for repeat/follow-on offerings

    # Prospectuses
    elif form.startswith('424B'):
        return '📖'  # Open book for offering documents

    # Foreign issuer annual reports
    elif form in ['20-F', '20-F/A', '40-F', '40-F/A']:
        return '🌐'  # Globe for international filings

    # Insider trading forms
    elif form in ['3', '4', '5', '3/A', '4/A', '5/A']:
        return '👥'  # People for insider/beneficial owner reports

    # Significant beneficial ownership reports
    elif form.startswith(('SC 13D', 'SC 13G')) or form in ['SC 13D/A', 'SC 13G/A']:
        return '🏰'  # Castle for large ownership stakes

    # Institutional investment holdings
    elif form in ['13F-HR', '13F-HR/A', '13F-NT', '13F-NT/A']:
        return '📈'  # Chart up for investment positions

    # Investment company reports
    elif form in ['N-CSR', 'N-CSR/A', 'N-Q', 'N-Q/A']:
        return '💼'  # Briefcase for investment portfolio

    # Proxy voting records
    elif form in ['N-PX', 'N-PX/A']:
        return '🗳️'  # Ballot box for voting records

    # Municipal advisor forms
    elif form in ['MA', 'MA/A', 'MA-I', 'MA-I/A']:
        return '⚖️'  # Scales for regulatory/compliance

    # SEC correspondence
    elif form in ['CORRESP', 'UPLOAD']:
        return '💬'  # Speech bubble for communications

    # Proxy statements
    elif form in ['DEF 14A', 'PRE 14A', 'DEFA14A', 'DEFC14A']:
        return '📩'  # Envelope for shareholder communications

    # Default case - generic document
    return '📄'

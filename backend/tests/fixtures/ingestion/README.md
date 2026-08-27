# Ingestion fixtures

The ingestion tests generate every fixture inside pytest's temporary directory.
This keeps malformed, encrypted-flag, traversal, high-compression, and large
workbook samples away from the repository and makes their construction explicit.

The generated corpus covers UTF-8/BOM and CP949 delimited text, XLS, XLSX,
XLSB package identification, ODS, merged cells, multiple sheets, leading notes,
blank rows, duplicate headers, formulas, broken workbooks, unsafe ZIP metadata,
and a 20,000-row XLSX benchmark sample.

## Redistributable XLSB corpus

`python-calamine-base.xlsb` is copied unchanged from
[`dimastbk/python-calamine` at commit `659a7e724f19f8c413122afec5b302e1ff5e04f5`](https://github.com/dimastbk/python-calamine/blob/659a7e724f19f8c413122afec5b302e1ff5e04f5/tests/data/base.xlsb).
That repository is distributed under the
[`MIT` license](https://github.com/dimastbk/python-calamine/blob/659a7e724f19f8c413122afec5b302e1ff5e04f5/LICENSE).
The fixture SHA-256 is
`b3a6b5034076373fcf2c93979839738275fac80b9de64ac557235f5a286f7e89`.

The tests also construct a small data-bearing BIFF12 workbook with Korean book
headers and mixed string/number values. Its OPC and BIFF12 framing is derived
from calamine's public minimal reproducer; the added cell records and values are
test-authored and deterministic.

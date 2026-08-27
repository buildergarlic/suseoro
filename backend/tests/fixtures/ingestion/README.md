# Ingestion fixtures

The ingestion tests generate every fixture inside pytest's temporary directory.
This keeps malformed, encrypted-flag, traversal, high-compression, and large
workbook samples away from the repository and makes their construction explicit.

The generated corpus covers UTF-8/BOM and CP949 delimited text, XLS, XLSX,
XLSB package identification, ODS, merged cells, multiple sheets, leading notes,
blank rows, duplicate headers, formulas, broken workbooks, unsafe ZIP metadata,
and a 20,000-row XLSX benchmark sample.

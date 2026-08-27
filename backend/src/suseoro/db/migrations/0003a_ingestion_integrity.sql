CREATE TRIGGER parser_runs_source_file_insert
BEFORE INSERT ON parser_runs
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM source_files WHERE sha256 = NEW.source_file_sha256
)
BEGIN SELECT RAISE(ABORT, 'parser run source file does not exist'); END;

CREATE TRIGGER parser_runs_source_file_update
BEFORE UPDATE OF source_file_sha256 ON parser_runs
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM source_files WHERE sha256 = NEW.source_file_sha256
)
BEGIN SELECT RAISE(ABORT, 'parser run source file does not exist'); END;

CREATE TRIGGER source_files_parser_runs_delete
BEFORE DELETE ON source_files
FOR EACH ROW WHEN EXISTS (
    SELECT 1 FROM parser_runs WHERE source_file_sha256 = OLD.sha256
)
BEGIN SELECT RAISE(ABORT, 'source file has parser runs'); END;

CREATE TRIGGER source_files_parser_runs_sha_update
BEFORE UPDATE OF sha256 ON source_files
FOR EACH ROW WHEN NEW.sha256 <> OLD.sha256 AND EXISTS (
    SELECT 1 FROM parser_runs WHERE source_file_sha256 = OLD.sha256
)
BEGIN SELECT RAISE(ABORT, 'source file has parser runs'); END;

CREATE UNIQUE INDEX idx_source_documents_unique_parse
    ON source_documents(source_file_id, role, parser_version);

CREATE UNIQUE INDEX idx_source_rows_unique_provenance
    ON source_rows(source_document_id, COALESCE(sheet_name, ''), source_row);

UPDATE parser_runs SET source_file_sha256 = source_file_sha256;

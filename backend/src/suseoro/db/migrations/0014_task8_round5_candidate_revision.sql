ALTER TABLE acquisition_workspaces
    ADD COLUMN candidate_collection_revision INTEGER NOT NULL DEFAULT 0
        CHECK (candidate_collection_revision >= 0);

CREATE TRIGGER candidate_collection_revision_insert
AFTER INSERT ON candidate_decisions
BEGIN
    UPDATE acquisition_workspaces
    SET candidate_collection_revision = candidate_collection_revision + 1
    WHERE id = NEW.workspace_id AND school_id = NEW.school_id;
END;

CREATE TRIGGER candidate_collection_revision_update
AFTER UPDATE ON candidate_decisions
BEGIN
    UPDATE acquisition_workspaces
    SET candidate_collection_revision = candidate_collection_revision + 1
    WHERE (id = OLD.workspace_id AND school_id = OLD.school_id)
       OR (id = NEW.workspace_id AND school_id = NEW.school_id);
END;

CREATE TRIGGER candidate_collection_revision_delete
AFTER DELETE ON candidate_decisions
BEGIN
    UPDATE acquisition_workspaces
    SET candidate_collection_revision = candidate_collection_revision + 1
    WHERE id = OLD.workspace_id AND school_id = OLD.school_id;
END;

import pytest

from guardian.contracts import DATABASE, API, CONFIG, analyze, classify_file, is_release_notes


# --- classify_file: each category matches its intended paths ---

@pytest.mark.parametrize(
    "path",
    [
        "migrations/0001_init.sql",
        "backend/migrations/0002_add_users.sql",
        "alembic/versions/abcdef123_add_index.py",
        "alembic.ini",
        "backend/alembic.ini",
        "prisma/schema.prisma",
        "schema.prisma",
    ],
)
def test_classifies_database_files(path):
    assert DATABASE in classify_file(path)


@pytest.mark.parametrize(
    "path",
    [
        "openapi.yaml",
        "docs/openapi.yml",
        "api/openapi-v2.json",
        "swagger.yaml",
        "spec/swagger.json",
        "protos/service.proto",
        "schema.graphql",
    ],
)
def test_classifies_api_files(path):
    assert API in classify_file(path)


@pytest.mark.parametrize(
    "path",
    [
        ".env.example",
        "backend/.env.example",
        "docker-compose.yml",
        "docker-compose.yaml",
        "docker-compose.prod.yml",
        "deploy/docker-compose.yml",
    ],
)
def test_classifies_config_files(path):
    assert CONFIG in classify_file(path)


@pytest.mark.parametrize(
    "path",
    [
        "src/app.py",
        "README.md",
        "guardian/main.py",
        "tests/test_contracts.py",
        ".env",  # not .env.example
        "docker-compose.override.txt",  # wrong extension
    ],
)
def test_does_not_classify_unrelated_files(path):
    assert classify_file(path) == []


# --- is_release_notes ---

@pytest.mark.parametrize(
    "path",
    [
        "CHANGELOG.md",
        "changelog.md",
        "release-notes/2024-01-01.md",
        "releases/v1.2.0.md",
        "docs/release-notes/v1.md",
    ],
)
def test_recognizes_release_notes_files(path):
    assert is_release_notes(path)


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "notes.md",
        "src/changelog_parser.py",
    ],
)
def test_does_not_recognize_non_release_notes_as_release_notes(path):
    assert not is_release_notes(path)


# --- analyze: the flagging decision ---

def test_flags_migration_without_release_notes():
    result = analyze(["migrations/0001_init.sql", "src/app.py"])
    assert result.flagged
    assert DATABASE in result.contract_files


def test_flags_api_schema_without_release_notes():
    result = analyze(["api/openapi.yaml"])
    assert result.flagged
    assert API in result.contract_files


def test_flags_config_change_without_release_notes():
    result = analyze([".env.example"])
    assert result.flagged
    assert CONFIG in result.contract_files


def test_does_not_flag_when_release_notes_also_changed():
    result = analyze(["migrations/0001_init.sql", "CHANGELOG.md"])
    assert not result.flagged
    assert result.release_notes_touched
    assert DATABASE in result.contract_files


def test_does_not_flag_unrelated_files_only():
    result = analyze(["src/app.py", "README.md"])
    assert not result.flagged
    assert result.contract_files == {}


def test_does_not_flag_release_notes_only_change():
    result = analyze(["CHANGELOG.md"])
    assert not result.flagged
    assert result.contract_files == {}
    assert result.release_notes_touched


def test_does_not_flag_empty_file_list():
    result = analyze([])
    assert not result.flagged
    assert result.contract_files == {}


def test_a_path_can_match_multiple_categories_and_still_group_correctly():
    result = analyze(["migrations/0001_init.sql", "openapi.yaml", "docker-compose.yml", "CHANGELOG.md"])
    assert not result.flagged
    assert set(result.contract_files.keys()) == {DATABASE, API, CONFIG}


# --- Renamed files: both filename and previous_filename must be checked ---

def test_rename_out_of_a_contract_path_is_still_flagged():
    # Moved from migrations/ to a path that alone wouldn't classify as
    # database. Without checking previous_filename this would be silently
    # missed even though the DB migration history changed.
    files = [{"filename": "archive/0001_init.sql", "previous_filename": "migrations/0001_init.sql"}]
    result = analyze(files)
    assert DATABASE in result.contract_files
    assert result.flagged


def test_rename_reports_the_current_filename_not_the_stale_one():
    files = [{"filename": "archive/0001_init.sql", "previous_filename": "migrations/0001_init.sql"}]
    result = analyze(files)
    assert result.contract_files[DATABASE] == ["archive/0001_init.sql"]


def test_rename_into_a_contract_path_is_flagged():
    files = [{"filename": "migrations/0001_init.sql", "previous_filename": "scratch/0001_init.sql"}]
    result = analyze(files)
    assert DATABASE in result.contract_files
    assert result.flagged


def test_rename_between_two_unrelated_paths_is_not_flagged():
    files = [{"filename": "src/new_name.py", "previous_filename": "src/old_name.py"}]
    result = analyze(files)
    assert result.contract_files == {}
    assert not result.flagged


def test_rename_without_previous_filename_key_behaves_like_plain_path():
    files = [{"filename": "migrations/0001_init.sql"}]
    result = analyze(files)
    assert DATABASE in result.contract_files
    assert result.flagged


def test_renamed_release_notes_file_counts_via_either_path():
    files = [
        {"filename": "migrations/0001_init.sql"},
        {"filename": "CHANGELOG.md", "previous_filename": "HISTORY.md"},
    ]
    result = analyze(files)
    assert result.release_notes_touched
    assert not result.flagged


def test_mixed_plain_strings_and_dict_entries_both_work():
    files = ["src/app.py", {"filename": "migrations/0001_init.sql", "previous_filename": None}]
    result = analyze(files)
    assert DATABASE in result.contract_files
    assert result.flagged

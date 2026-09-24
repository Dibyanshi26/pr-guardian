from guardian.overlap import PROverlap, find_overlaps


def test_no_overlap_when_no_files_shared():
    overlaps = find_overlaps(
        this_files=["src/app.py"],
        other_prs=[(42, "unrelated change", ["src/other.py"])],
    )
    assert overlaps == []


def test_overlap_on_a_plain_shared_path():
    overlaps = find_overlaps(
        this_files=["src/app.py", "README.md"],
        other_prs=[(42, "also touches app.py", ["src/app.py"])],
    )
    assert overlaps == [PROverlap(pr_number=42, title="also touches app.py", shared_files=["src/app.py"])]


def test_no_overlap_with_a_pr_that_touches_no_shared_files():
    overlaps = find_overlaps(
        this_files=["src/app.py"],
        other_prs=[(42, "some other pr", ["src/other.py", "README.md"])],
    )
    assert overlaps == []


def test_multiple_overlapping_prs_are_all_reported_sorted_by_number():
    overlaps = find_overlaps(
        this_files=["src/app.py"],
        other_prs=[
            (99, "later pr", ["src/app.py"]),
            (5, "earlier pr", ["src/app.py"]),
        ],
    )
    assert [o.pr_number for o in overlaps] == [5, 99]


def test_shared_files_within_one_overlap_are_sorted():
    overlaps = find_overlaps(
        this_files=["z.py", "a.py"],
        other_prs=[(42, "touches both", ["a.py", "z.py"])],
    )
    assert overlaps[0].shared_files == ["a.py", "z.py"]


# --- Renamed files: overlap must be found via old or new path, and always
# reported using this PR's current filename (mirrors contracts.py's rule) ---


def test_rename_out_of_a_path_still_overlaps_a_pr_touching_the_old_path():
    # This PR moved config.py out of settings/; another open PR still
    # touches the original settings/config.py path.
    this_files = [{"filename": "config.py", "previous_filename": "settings/config.py"}]
    overlaps = find_overlaps(
        this_files=this_files,
        other_prs=[(7, "touches old path", ["settings/config.py"])],
    )
    assert len(overlaps) == 1
    assert overlaps[0].shared_files == ["config.py"]  # current filename, not the stale one


def test_rename_into_a_path_overlaps_a_pr_touching_the_new_path():
    this_files = [{"filename": "settings/config.py", "previous_filename": "config.py"}]
    overlaps = find_overlaps(
        this_files=this_files,
        other_prs=[(7, "touches new path", ["settings/config.py"])],
    )
    assert overlaps[0].shared_files == ["settings/config.py"]


def test_rename_between_two_unrelated_paths_does_not_falsely_overlap():
    this_files = [{"filename": "src/new_name.py", "previous_filename": "src/old_name.py"}]
    overlaps = find_overlaps(
        this_files=this_files,
        other_prs=[(7, "unrelated", ["src/unrelated.py"])],
    )
    assert overlaps == []


def test_overlap_reported_using_this_prs_current_filename_when_other_pr_also_renamed():
    # Both PRs touch the same file's history, but under different names on
    # each side. The overlap should still be found (via the shared old
    # path) and reported using THIS PR's current name.
    this_files = [{"filename": "archive/legacy.py", "previous_filename": "src/legacy.py"}]
    other_files = [{"filename": "src/legacy_v2.py", "previous_filename": "src/legacy.py"}]
    overlaps = find_overlaps(
        this_files=this_files,
        other_prs=[(7, "also renamed it", other_files)],
    )
    assert overlaps[0].shared_files == ["archive/legacy.py"]


def test_mixed_plain_strings_and_dict_entries_both_work():
    overlaps = find_overlaps(
        this_files=["src/app.py", {"filename": "migrations/0001.sql", "previous_filename": None}],
        other_prs=[(7, "touches migration", ["migrations/0001.sql"])],
    )
    assert overlaps[0].shared_files == ["migrations/0001.sql"]

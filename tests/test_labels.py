"""Agent labels follow the directory, per repository."""

from __future__ import annotations

from exradius_github.exradius import Directory
from exradius_github.labels import resolve, wanted
from tests.conftest import World


async def test_labels_are_created_where_the_app_is_installed_and_the_project_runs(world: World):
    assert world.github.label_names("acme/plane") == {
        "bug": "Something isn't working",
        "agent: Developer": "Exradius plane/developer",
        "agent: Scout": "Exradius plane/scout",
    }
    assert world.github.label_names("acme/saleor") == {}  # Saleor is not runnable
    assert world.github.label_names("acme/elsewhere") == {}  # not in the directory


async def test_removed_agents_lose_their_label_and_renames_follow(world: World):
    world.github.add_issue("acme/plane", 1, "x")
    world.github.label_issue("acme/plane", 1, "agent: Scout")
    projects = world.directory.directory["projects"]
    projects[0]["agents"] = [{"slug": "developer", "name": "Engineer"}]
    projects[1]["runnable"] = True
    await world.bridge.refresh_directory()

    assert world.github.label_names("acme/plane") == {"bug": "Something isn't working", "agent: Engineer": "Exradius plane/developer"}
    assert world.github.issue_labels("acme/plane", 1) == []
    assert world.github.label_names("acme/saleor") == {"agent: Developer": "Exradius saleor/developer"}


async def test_a_repo_that_leaves_the_directory_loses_its_labels(world: World):
    del world.directory.directory["projects"][0]
    await world.bridge.refresh_directory()
    assert world.github.label_names("acme/plane") == {"bug": "Something isn't working"}


async def test_a_label_of_the_same_name_that_is_not_ours_is_left_alone(world: World):
    world.github.add_issue("acme/plane", 1, "x")
    world.github.repos["acme/plane"]["labels"] = [{"id": 1, "name": "agent: Developer", "description": "the team's own", "color": "000000"}]
    await world.bridge.refresh_directory()
    names = world.github.label_names("acme/plane")
    assert names["agent: Developer"] == "the team's own"
    assert names["agent: Scout"] == "Exradius plane/scout"


def directory(*projects) -> Directory:
    return Directory.model_validate({"projects": list(projects)})


def project(slug, name, repos, agents, runnable=True) -> dict:
    return {
        "slug": slug, "name": name, "runnable": runnable,
        "repositories": [{"name": r.split("/")[-1], "url": f"https://github.com/{r}.git"} for r in repos],
        "agents": [{"slug": s, "name": n} for s, n in agents],
    }


def test_a_repo_in_several_projects_names_the_project_and_long_names_are_cut():
    d = directory(
        project("plane", "Plane", ["acme/shared"], [("developer", "Developer")]),
        project("plane-2", "Plane", ["acme/shared"], [("developer", "A developer with a very long name indeed, really")]),
    )
    names = [w.name for w in wanted(d, "github.com")["acme/shared"]]
    assert names[0] == "agent: Plane (plane) · Developer"
    assert len(names[1]) == 50 and names[1].endswith("…")


def test_resolve_checks_the_repo_belongs_to_the_project():
    d = directory(project("plane", "Plane", ["acme/plane"], [("developer", "Developer")]))
    label = {"name": "agent: Developer", "description": "Exradius plane/developer"}
    ok = resolve(label, "Acme/Plane", d, "github.com")
    assert ok and ok.choice and (ok.choice.project, ok.choice.agent, ok.choice.shown) == ("plane", "developer", "Plane · Developer")
    assert resolve(label, "acme/other", d, "github.com").kind == "unavailable"
    assert resolve({"name": "bug", "description": "Something"}, "acme/plane", d, "github.com") is None

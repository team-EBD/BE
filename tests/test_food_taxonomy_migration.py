import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import CheckConstraint, MetaData, create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.models import Base


def test_migration_normalizes_legacy_families_and_enforces_contract():
    path = Path(__file__).parents[1] / "alembic/versions/2026_09_16_1100-e2f85abc9d46_food_taxonomy_constraints.py"
    spec = importlib.util.spec_from_file_location("food_taxonomy_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    legacy = MetaData()
    for table in Base.metadata.sorted_tables:
        copy = table.to_metadata(legacy)
        for constraint in list(copy.constraints):
            if isinstance(constraint, CheckConstraint) and constraint.name.startswith(
                ("ck_food_groups_", "ck_food_group_aliases_", "ck_nutrition_items_serving_basis")
            ):
                copy.constraints.remove(constraint)
    engine = create_engine("sqlite://")
    legacy.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO food_groups (id,name,family,role,companion_group_id) "
                                "VALUES (1,'닭가슴살','육·수산 가공','exclude',1)"))
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        row = connection.execute(text("SELECT family,companion_group_id FROM food_groups WHERE id=1")).one()
        assert row == ("육류·수산물·달걀", None)
        assert len(inspect(connection).get_check_constraints("food_groups")) == 4
        for sql in (
            "UPDATE food_groups SET family='잘못된분류' WHERE id=1",
            "UPDATE food_groups SET role='invalid' WHERE id=1",
            "UPDATE food_groups SET member_count=-1 WHERE id=1",
            "UPDATE food_groups SET companion_group_id=id WHERE id=1",
            "INSERT INTO food_group_aliases(alias,group_id,kind) VALUES ('별칭',1,'bad')",
        ):
            with pytest.raises(IntegrityError):
                connection.execute(text(sql))
        migration.downgrade()
        assert connection.scalar(text("SELECT family FROM food_groups WHERE id=1")) == "육·수산 가공"
        migration.upgrade()
    engine.dispose()

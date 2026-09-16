import _bootstrap  # noqa: F401
from core.data.profile import verify_profiles
from core.data.store import Store


if __name__ == "__main__":
    with Store() as store:
        result = verify_profiles(store)
        print(f"L0 passed: {result['table_count']} tables / {result['core_table_count']} core tables; source issue groups: {len(result['issues'])}")

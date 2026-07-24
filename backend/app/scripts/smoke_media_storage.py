from app.services.media_storage import get_media_storage


def main() -> None:
    storage = get_media_storage()
    storage.check()
    print(f"media storage smoke test passed: backend={storage.backend}")


if __name__ == "__main__":
    main()

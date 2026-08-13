import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CPU_COMPOSE = PROJECT_ROOT / "docker-compose.embedding-cpu.yml"


class EmbeddingCpuComposeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.compose = CPU_COMPOSE.read_text(encoding="utf-8")

    def test_embedding_is_published_only_on_the_host_loopback_interface(self):
        self.assertIn(
            'ports:\n      - "127.0.0.1:8080:80"',
            self.compose,
        )
        self.assertNotIn('"0.0.0.0:8080:80"', self.compose)
        self.assertNotIn('\n      - "8080:80"', self.compose)


if __name__ == "__main__":
    unittest.main()

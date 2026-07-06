from elasticsearch import Elasticsearch

from app.core.config import settings

es_client = Elasticsearch(settings.ES_URL)

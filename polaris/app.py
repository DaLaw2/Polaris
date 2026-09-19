"""Polaris API — search & classification backend."""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from polaris import config, state
from polaris.catalog import api as catalog_api
from polaris.derivation import api as derivation_api
from polaris.jobs import api as jobs_api
from polaris.jobs import worker
from polaris.models import api as models_api
from polaris.observation import api as observation_api
from polaris.search import api as search_api
from polaris.search.engine import SearchEngine
from polaris.vocabulary import api as vocabulary_api
from polaris.vocabulary.service import EntityManager


@asynccontextmanager
async def lifespan(app: FastAPI):
    await state.store.connect()

    state.entity_mgr = EntityManager(state.store)
    await state.entity_mgr.init_schema()

    state.engine = SearchEngine(state.store, vocabulary=state.entity_mgr)

    async with state.store.acquire() as conn:
        await config.load_collections(conn)

    derives = asyncio.create_task(worker.serve_derive(state.store))
    yield

    derives.cancel()
    await state.store.close()


app = FastAPI(title="Polaris API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(catalog_api.router)
app.include_router(search_api.router)
app.include_router(vocabulary_api.router)
app.include_router(jobs_api.router)
app.include_router(derivation_api.router)
app.include_router(models_api.router)
app.include_router(observation_api.router)

# Memory System

The Phase 3 memory subsystem gives NovaControl a production-shaped memory boundary with local durable storage and future vector database support.

## Namespaces

- `conversation`: conversational context and preferences
- `project`: project state, milestones, and decisions
- `knowledge`: durable research and reference knowledge
- `short_term`: expiring working memory
- `long_term`: durable user and system memory

## Components

- `MemoryRecord`: typed immutable record with metadata, importance, timestamps, and optional expiration
- `MemoryManager`: high-level remember, recall, retrieve, summarize, and cleanup API
- `MemoryModule`: event-driven runtime module for memory write and cleanup events
- `InMemoryMemoryStore`: fast process-local store for tests and embedded use
- `SqliteMemoryStore`: local durable store using the Python standard library
- `VectorIndex`: interface for vector database adapters
- `HashingVectorIndex`: dependency-free lexical similarity index for development and tests
- `ExtractiveMemorySummarizer`: deterministic summarizer used until an LLM summarizer is configured

## Events

- `memory.remember`: writes a memory record
- `memory.stored`: emitted after a record is stored
- `memory.cleanup_requested`: removes expired records
- `memory.cleanup_completed`: emitted after cleanup

## Future Adapters

The vector interface is intentionally narrow so Chroma, pgvector, Redis, FAISS, or cloud vector stores can be added without changing the manager or core runtime.

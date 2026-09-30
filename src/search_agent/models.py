from pydantic import BaseModel, Field, field_validator


class RawSearchResult(BaseModel):
    """A single result from the configured search provider."""

    title: str
    url: str
    snippet: str
    engine: str
    content: str | None = None
    # Advisory recency signal (set by providers that report it, e.g. Staan).
    # Passed through to the synthesizer in the results JSON; not all providers
    # populate it.
    published_date: str | None = None

    @field_validator("title")
    @classmethod
    def truncate_title(cls, v: str) -> str:
        return v[:200] if len(v) > 200 else v

    @field_validator("snippet")
    @classmethod
    def truncate_snippet(cls, v: str) -> str:
        return v[:500] if len(v) > 500 else v


class PiiCheck(BaseModel):
    """Structured verdict from the PII gate.

    `reason` is for logs/operators only — the gate prompt instructs the model
    to name the PII category generically and never echo the personal data
    itself into this field.
    """

    contains_pii: bool
    reason: str = ""


class Source(BaseModel):
    """A cited source in the final output."""

    title: str
    url: str


class SearchResult(BaseModel):
    """Final pipeline output — sourced summary with citations."""

    summary: str
    sources: list[Source]


class SearchRequest(BaseModel):
    """Incoming search request from the backend."""

    query: str = Field(..., min_length=1, max_length=2000)
    context: str = Field(default="", max_length=10000)
    no_cache: bool = False

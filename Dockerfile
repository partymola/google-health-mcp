# The patch tag, not `3.14-slim`. Dependabot treats the tag as the version and
# will not open a pull request for a version one already exists for, counting
# closed ones - which stopped digest updates dead in the four sibling servers
# that closed their `3.13-slim -> 3.14-slim` pull request. This repo never had
# one and its updates still arrive, so the pin here is for consistency and to
# keep it out of that trap rather than to escape it.
FROM python:3.15.2-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928

WORKDIR /app

# Copy package metadata and source
COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/

# Install the package (no dev deps needed for runtime)
RUN pip install --no-cache-dir .

# MCP server uses stdio transport - no port to expose
ENTRYPOINT ["google-health-mcp"]

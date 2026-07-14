# Pinned to the exact Python version this project was built and tested
# against locally (3.14.4). audioop was removed from the stdlib in
# Python 3.13+ (see requirements.txt's audioop-lts note) -- staying on
# the same major.minor as local dev avoids introducing an untested
# runtime this late in the project, rather than dropping to an older
# Python purely for wheel-availability convenience.
FROM python:3.14-slim

WORKDIR /app

# build-essential is a defensive inclusion: Python 3.14 is new enough
# that some C-extension dependencies (uvicorn's uvloop/httptools) may
# not yet have prebuilt wheels for it on every platform, which would
# otherwise make pip try to compile from source and fail with no
# compiler present. curl is needed for the container HEALTHCHECK.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl \
    && rm -rf /var/lib/apt/lists/*

# Copied and installed before the rest of the app so Docker's layer
# cache is only invalidated when dependencies actually change, not on
# every code edit -- makes rebuilds during iteration much faster.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["python", "run.py"]

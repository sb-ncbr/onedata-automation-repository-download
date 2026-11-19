FROM onedata/lambda-base-slim:v3

ARG LAMBDA_PACKAGE=

USER root
RUN --mount=type=cache,target=/home/app/.cache/uv \
    --mount=type=bind,source=.,target=/build \
    build-lambda "$LAMBDA_PACKAGE"
USER app

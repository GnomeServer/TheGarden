FROM ubuntu:24.04

ARG OMP_VERSION=18.12.5
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates curl git python3 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 omp

USER omp
ENV HOME=/home/omp \
    PATH=/home/omp/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
RUN curl -fsSL https://omp.sh/install | sh -s -- --binary --ref "v${OMP_VERSION}"

USER root
RUN test -x /home/omp/.local/bin/omp \
    && chmod 0755 /home/omp /home/omp/.local /home/omp/.local/bin /home/omp/.local/bin/omp
WORKDIR /opt/omp-worker
COPY --chown=root:root entrypoint.py planner.md models.yml /opt/omp-worker/
COPY --chown=root:root agents/ /opt/omp-worker/agents/
RUN chmod -R a+rX /opt/omp-worker

USER 10001:10001
WORKDIR /work
ENTRYPOINT ["python3", "/opt/omp-worker/entrypoint.py"]

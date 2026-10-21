FROM python:3.12-slim

RUN useradd --create-home --uid 1000 crew \
    && mkdir -p /work \
    && chown crew:crew /work

WORKDIR /opt/crew
COPY omp.py planner.py coder.py reviewer.py supervisor.py /opt/crew/

USER crew
WORKDIR /work
ENTRYPOINT ["python", "/opt/crew/omp.py"]

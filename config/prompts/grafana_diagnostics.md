# Grafana Diagnostics — Analysis Prompt

You have been given a set of Prometheus metrics exported from Grafana covering
a window of interest (e.g. an incident or stress-test window).

Analyse the metrics and produce a report covering:

1. **CPU saturation** — identify which process or pod is the top consumer.
2. **Memory pressure** — heap utilisation, GC pause frequency and duration.
3. **Query queue depth** — average and peak queued query count.
4. **Network I/O** — bytes in/out; flag any outlier container.
5. **Disk I/O** — read/write IOPS and latency; flag spill to disk events.
6. **Bottleneck summary** — rank the top 3 bottlenecks by severity.
7. **Recommended actions** — specific tuning changes, resource adjustments, or alerts to add.

# Project Brief - Original Context

Build an MVP for Kroger sourcing category recommendations across three workloads:

1. Workload 1: Next.js/React spend visibility dashboard and user chat UI.
2. Workload 2: FastAPI backend exposing spend, invoice, supplier, opportunity, chart, governance, and chat APIs.
3. Workload 3: Python LangGraph spend recommendation agent using sanitized/query tools and external data seams.

The attached architecture calls for PING SAML/OIDC user authentication, HTTPS ingress, protected service boundaries, Azure AI Content Safety, observability, Redis caching, cloud-managed data integrations, and AKS deployment. For this MVP, auth issuer/client endpoints and deployment endpoints may remain placeholders for dev, stage, and prod. Local development must remain reproducible.

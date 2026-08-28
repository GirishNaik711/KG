## Problem Statement
Kroger sourcing users need a clearer way to understand spend, suppliers, invoice quality, unmanaged coverage, and sourcing opportunities across multiple systems. Today the MVP has three separate workloads but lacks a single documented discovery baseline covering the Next.js dashboard, FastAPI service, and LangGraph recommendation agent. The attached architecture also calls for PING SAML/OIDC authentication, protected HTTPS boundaries, content safety, observability, caching, and cloud deployment, while identity and environment endpoints are not yet available.

## Solution
The MVP keeps three separate workloads: Workload 1 is a Next.js/React dashboard and chat UI; Workload 2 is a FastAPI service exposing REST JSON data and chat-compatible streaming boundaries; and Workload 3 is a Python LangGraph spend recommendation agent. The frontend uses an Auth.js OIDC scaffold for PING, with dev, stage, and prod issuer and API endpoints represented as placeholders and gateway/IAP enforcement deferred until identity details are provided. The system is cloud-targeted toward AKS while retaining local startup and deterministic non-Docker validation. It persists source-backed spend data and uses Redis and cloud integration seams, and the agent applies input sanitization, content safety, and observability controls. Operational signals include access logs, API metrics, runtime errors, and WAF events. The compliance selection currently records SOC 2, GDPR, HIPAA, and PCI-DSS for follow-up reconciliation before production.

## User Stories
1. As a sourcing user, I want to filter and inspect spend records, so that I can identify unmanaged spend and data-quality issues.
2. As a sourcing user, I want to ask the spend recommendation agent questions, so that I can find supplier and category opportunities faster.
3. As an application operator, I want frontend, backend, and agent workloads to have explicit API and security boundaries, so that each workload can be validated and deployed independently.
4. As a security administrator, I want PING OIDC authentication and protected service boundaries represented in the design, so that identity integration can be completed when environment details are available.
5. As an operator, I want access, API, runtime, and WAF signals captured, so that failures and policy violations can be investigated.

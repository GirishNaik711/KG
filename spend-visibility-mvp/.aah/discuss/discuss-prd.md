# Discuss PRD - Spend Visibility MVP

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

## Overview
- **Project:** spend-visibility-mvp
- **Delivery intent:** MVP
- **Domains:** None selected
- **Technical archetype:** ai-applications
- **Workloads:** Next.js frontend, FastAPI backend, Python LangGraph agent

## Scope
The Discuss scope covers all three application workloads and the boundaries between them. It includes frontend identity scaffolding, backend REST and chat contracts, agent sanitization and governance, source-backed data and Redis/cache seams, cloud deployment assumptions, and operational observability. It excludes production identity values, final gateway ownership, final deployment target confirmation, and final compliance certification.

## Decisions Made

| slug_id | Area | Decision | Source |
|---|---|---|---|
| cloud-vs-local | Architecture | Cloud-targeted, with local startup retained | user |
| data-storage-present | Data & state architecture | Persistent source data and cache seams are in scope | user |
| custom-ui-required | UI / UX | Custom Next.js dashboard and chat UI required | user |
| compliance-regimes | Security, privacy & compliance | SOC 2, GDPR, HIPAA, and PCI-DSS recorded for reconciliation | user |
| docker-installed | Architecture | No local Docker runtime; use deterministic fallback validation | user |
| workload-boundaries | Architecture & technical foundation | Frontend UI, FastAPI backend, and LangGraph agent remain separate workloads | user |
| backend-contract | Integration & external interfaces | REST JSON plus chat-compatible streaming boundary | user |
| agent-governance | Agentic AI development | Input sanitization, content safety, and observability | user |
| deployment-observability | Deployment, Ops & observability | Access logs, API metrics, runtime errors, and WAF events | user |
| cloud-provider | Architecture & technical foundation | AKS in us-east4 as the current target, subject to confirmation | user |
| development-methodology | Architecture & technical foundation | Incremental MVP delivery | user |

## Pre-Resolved

| slug_id | Decision | Source |
|---|---|---|
| runtime-stack | FastAPI backend, Next.js frontend, Python LangGraph agent as three separate workloads | codebase |
| frontend-auth-boundary | Auth.js OIDC provider with PING issuer placeholders; gateway/IAP enforcement deferred | user |

## Constraints & Compliance
- Authentication values for dev, stage, and prod remain placeholders until PING identity details are available.
- The frontend auth scaffold must not be treated as production gateway enforcement.
- The three workloads must remain independently deployable and testable.
- Local validation must work without Docker.
- The selected compliance set is intentionally broad and must be reconciled before production.
- The cloud target, ingress, WAF, and IAP ownership require final confirmation.

## Open Questions
- What are the PING issuer, client, redirect, scopes, and claims for each environment?
- Is identity enforcement owned by PING, an application gateway, IAP, or a combination?
- Is AKS in us-east4 the final hosting target, and which provider owns that environment?
- Which compliance regimes actually apply to this spend data and user population?
- Which external Oracle AP, Coupa, EAA, Databricks MCP, Redis, and model endpoints will be provisioned for each environment?

## Constraint Audit
- **Status:** PASS
- **Mandatory coverage:** PASS
- **Forbidden options:** PASS
- **Whitelist adherence:** PASS
- **Service scope:** PASS
- **Mandated-option enforcement:** PASS
- **Elimination provenance:** PASS

# M1: Spend Visibility Requirements

## Product Context

Build a unified spend visibility capability for GNFR spend. Oracle AP is the base layer for 100% of spend, augmented by Coupa and EAA where matching detail exists. Coupa is currently the only source with a validated Bronze-to-Gold object map. Equivalent Oracle AP and EAA object and relationship maps remain open dependencies.

## Epic 1: Unified Spend Foundation

Establish Oracle AP as the base layer for complete GNFR visibility, augmented by Coupa and EAA detail without losing Oracle-only coverage.

### Story 1.1: Baseline Spend Visibility from Oracle AP

As a Sourcing Associate, I want to see total GNFR spend by supplier, category, and location (division, manufacturing plant, distribution center) even when a transaction exists only in Oracle AP, so I have a baseline view across all spend.

Acceptance criteria:
- Oracle-only GNFR transactions are included in supplier, category, and location totals.
- Oracle AP totals reconcile against source records.
- Location drill-down supports division, manufacturing plant, and distribution center levels.

Out of scope: site/store granularity below division; Coupa/EAA enrichment; an undocumented Oracle AP Bronze/Silver/Gold map.

### Story 1.2: Coupa/EAA Enrichment

As a Sourcing Associate, I want spend records enriched with Coupa/EAA supplier, contract, category, and invoice detail when available, so I get richer context without losing Oracle-only coverage.

Acceptance criteria:
- Matching Coupa/EAA detail is displayed alongside the Oracle AP transaction.
- Oracle-only transactions remain visible without enrichment.
- Enriched fields are visually distinguished with their source, such as `via Coupa` or `via EAA`.

Out of scope: conflict precedence rules and enrichment beyond Coupa/EAA.

### Story 1.3: Source Precedence Rule

As a Data/AI Engineer, I want source-system detail to override Oracle AP where both exist, so spend is not double-counted or contradicted.

Acceptance criteria:
- Source-system values are used over Oracle AP values when the same transaction exists in both.
- Overlapping transactions are counted once.
- The winning source and resolution are auditable.

Open question: precedence between Coupa and EAA when both conflict.

## Epic 2: Multi-Signal Categorization and Taxonomy

Build reliable categorization using a confirmed default taxonomy and supplementary signals.

### Story 2.1: Default Categorization Taxonomy

As a Data/AI Engineer, I want Coupa spend categorized against a confirmed default taxonomy, so category is consistently available. Taxonomy choice is currently TBD between the Coupa commodity hierarchy and a business-provided taxonomy; do not treat this as finalized.

Acceptance criteria (pending decision):
- Coupa spend receives a category using the confirmed taxonomy.
- Taxonomy updates can be reflected without redeploying.
- Category is available for filtering and drill-down.

Out of scope: Oracle AP-only or EAA-only categorization, supplementary signals, and full business taxonomy rollout.

### Story 2.2: Low-Confidence Categorization Flagging

As a Sourcing Associate, I want spend that cannot be confidently categorized flagged rather than silently miscategorized, so I know which numbers to trust.

Acceptance criteria:
- Records below a defined, configurable threshold are visibly flagged.
- Threshold changes update behavior without redeployment.
- The reason, such as no clean taxonomy match or conflicting signals, is available.

Out of scope: correction or rerouting. Threshold remains TBD.

### Story 2.3: Additional Categorization Signals

As a Data/AI Engineer, I want GL distribution descriptions, Supplier Hub category/subcategory, and NAICS codes available as supplementary categorization signals.

Acceptance criteria:
- GL line-item description is available when the primary taxonomy match is weak, subject to access.
- Supplier Hub category/subcategory and NAICS are available when a supplier match exists.
- Signal priority and hierarchy are documented and auditable.

Out of scope: securing GL access, OCR invoice detail, and Supplier Hub data-quality remediation.

### Story 2.4: Categorization Feedback Loop

As a Sourcing Associate, I want to confirm or correct a spend record category so the tool can improve future categorization accuracy.

Acceptance criteria:
- A user can confirm or reject a category with minimal friction.
- Rejection allows selection of the correct category.
- Feedback is captured as structured data for later retraining.

Out of scope: real-time retraining and bulk correction.

## Epic 3: Shared Spend View and Drill-Through

### Story 3.1: Full Spend Population with Manual Filtering

As a Sourcing Associate, I want the full spend population by default and manual filters by category, supplier, and location, so I can focus on what is relevant.

Acceptance criteria:
- No filters shows all spend across categories.
- Category, supplier, and location filters narrow to matching records.
- Sourcing Associates, COE users, and Executive Sponsors see the same unrestricted view and filter options.

Out of scope: persona-based pre-filtering, role-specific landing views, saved preferences, and traversal to contracts/invoices.

### Story 3.2: Ontology-Style Drill-Through

As a Sourcing Associate, I want to navigate from a filtered supplier or category to related contracts and then invoices without re-querying from scratch.

Acceptance criteria:
- Selecting a filtered supplier or category shows related contracts.
- Selecting a contract shows related invoices.
- Back navigation preserves the prior place and filters.
- The interaction uses a familiar related-object and back-navigation pattern.

Out of scope: location-to-contract traversal, OCR line-item detail, role-specific entry points, and pixel-level replication of another product.

## Epic 4: Managed and Unmanaged Spend

Distinguish spend covered by an active contract from spend outside contract coverage.

Acceptance criteria:
- Users can view managed versus unmanaged spend.
- Users can drill from the split to suppliers and transactions driving unmanaged spend.
- Location/division can be combined with category and supplier filtering.

## Epic 5: Data Quality and Trust Signals

### Story 5.1: Spend Data Freshness

As a Sourcing Associate, I want to see how recently spend was refreshed, so I know whether data is current or stale.

Acceptance criteria:
- Spend figures show an actual timestamp or relative freshness indicator.
- Combined views reflect the least-recently-refreshed contributing source.
- The indicator reflects actual source currency rather than a static label.

Out of scope: defining refresh cadence and real-time streaming.

### Story 5.2: Data Quality Issue Flagging

As a Sourcing Associate, I want records with missing PO, missing supplier, or broken relationships flagged, so I do not act on unreliable numbers.

Acceptance criteria:
- Missing PO references are visibly flagged.
- Missing supplier links are visibly flagged.
- Broken relationships, such as invoice-to-PO, are flagged.
- The specific issue type is identifiable.

Out of scope: automatic remediation and categorization-confidence flags.

### Story 5.3: Data Quality Gating for AI Recommendations

As a Data/AI Engineer, I want data-quality issues to block or qualify downstream AI recommendations, so bad data does not silently propagate into sourcing suggestions.

Acceptance criteria:
- Recommendations based on flagged records are suppressed or carry a visible caveat.
- The specific underlying issue is identifiable.
- Resolved issues cause recommendations to be re-evaluated without the caveat.

Out of scope: recommendation logic and automatic issue remediation.

## Cross-Cutting Dependencies and Decisions

- Confirm the default taxonomy before implementing Story 2.1.
- Document Oracle AP and EAA object/relationship maps.
- Obtain GL distribution-table access.
- Resolve Coupa-versus-EAA precedence conflicts.
- Confirm source refresh cadence.
- Use representative Oracle AP, Coupa, EAA, supplier, contract, invoice, taxonomy, and data-quality data for the first vertical slice.

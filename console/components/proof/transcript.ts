// Captured verbatim from scripts/gateway_e2e.py run against the live public
// stack (datahub.onenept.com) on Jul 18 2026, exit code 0. Also committed at
// public/proof/gateway-transcript.txt.
export const GATEWAY_TRANSCRIPT = `PASS  tool surface mirrored exactly | 20 tools
PASS  reads carry trust context
PASS  implicit claim recorded on write | confidence=0.6
PASS  write forwarded to catalog
PASS  steward revert restored accepted text
PASS  implicit claim settled as wrong
      rogue-agent trust after revert: 51.3
PASS  settled record pulls trust under the floor | 51.3 < 55.0
PASS  enforce blocks the low-trust write | MCP tool update_description failed: heimdall policy: agent 'rogue-agent' trust 51.3 is below the floor
PASS  reads still allowed under enforce
PASS  blocked write recorded no claim
PASS  blocked write never reached the catalog

ALL CHECKS PASSED`;

import unittest

from torrent_vpn import watch as w


class KillSwitchPlanTest(unittest.TestCase):
    def test_fresh_host_gets_chain_then_jump(self):
        cmds = w.kill_switch_plan([], ["-N DOCKER-USER"], ["172.19.0.2"])
        self.assertEqual(cmds[0], ["-N", w.CHAIN])
        self.assertEqual(cmds[-2], ["-A", w.CHAIN, "-j", "DROP"])
        self.assertEqual(cmds[-1], ["-I", "DOCKER-USER", "1", "-s", "172.19.0.2/32", "-j", w.CHAIN])

    def test_in_sync_is_a_no_op(self):
        du = ["-N DOCKER-USER", w.jump_rule("172.19.0.2")]
        self.assertEqual(w.kill_switch_plan(w.chain_rules(), du, ["172.19.0.2"]), [])

    def test_new_container_ip_is_added_before_the_stale_one_goes(self):
        du = ["-N DOCKER-USER", w.jump_rule("172.19.0.2")]
        cmds = w.kill_switch_plan(w.chain_rules(), du, ["172.19.0.9"])
        self.assertEqual(cmds, [["-I", "DOCKER-USER", "1", "-s", "172.19.0.9/32", "-j", w.CHAIN],
                                ["-D", "DOCKER-USER", "-s", "172.19.0.2/32", "-j", w.CHAIN]])

    def test_chain_drops_everything_but_private_and_tunnel(self):
        rules = w.chain_rules()
        self.assertEqual(rules[-1], f"-A {w.CHAIN} -j DROP")
        self.assertIn(f"-A {w.CHAIN} -o {w.TUNNEL} -j RETURN", rules)


class ClassifyTest(unittest.TestCase):
    def test_stopped_incomplete_is_pending_unless_held(self):
        ts = [{"hash": "a", "state": "stoppedDL", "tags": ""},
              {"hash": "b", "state": "stoppedDL", "tags": "tv, hold"},
              {"hash": "c", "state": "stoppedUP", "tags": ""}]
        self.assertEqual(w.classify(ts), (["a"], False))

    def test_downloading_or_seeding_is_active(self):
        self.assertEqual(w.classify([{"hash": "a", "state": "stalledDL"}]), ([], True))
        self.assertEqual(w.classify([{"hash": "a", "state": "uploading"}]), ([], True))
        self.assertEqual(w.classify([]), ([], False))


class TailscaleBypassTest(unittest.TestCase):
    def test_tailnet_traffic_gets_both_mullvad_exclusion_marks(self):
        rules = [ln.strip() for ln in w.TS_RULESET.splitlines() if "mark set" in ln]
        self.assertEqual(len(rules), 5)
        self.assertTrue(any(r.startswith("ip daddr 100.64.0.0/10") for r in rules))
        self.assertTrue(any(r.startswith('iifname "tailscale0"') for r in rules))
        # tailscaled's own packets (socket mark 0x80000) and inbound WireGuard UDP
        self.assertTrue(any(r.startswith("meta mark & 0x00ff0000 == 0x00080000") for r in rules))
        self.assertTrue(any(r.startswith('iifname != "wg0-mullvad" udp dport 41641') for r in rules))
        for r in rules:
            self.assertIn("ct mark set 0x00000f41 meta mark set 0x6d6f6c65", r)


if __name__ == "__main__":
    unittest.main()

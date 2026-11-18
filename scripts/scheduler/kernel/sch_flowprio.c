// SPDX-License-Identifier: GPL-2.0
/* Logical TrafficGenerator flows on an OVS system-port egress.
 * Experimental IPv4/TCP scheduler. Metadata is little-endian (the existing
 * generator's x86 wire format). No packet data or TCP checksums are modified.
 * Runtime control is root-only /proc/net/flowprio; see scheduler/README.md.
 */
#ifdef FLOWPRIO_USER_TEST
#include "../tests/kernel_shim.h"
#else
#include <linux/module.h>
#include <linux/hashtable.h>
#include <linux/ip.h>
#include <linux/tcp.h>
#include <linux/seq_file.h>
#include <linux/proc_fs.h>
#include <linux/uaccess.h>
#include <linux/mutex.h>
#include <asm/unaligned.h>
#include <net/pkt_sched.h>
#include <net/ip.h>
#include <net/sch_generic.h>
#include <net/net_namespace.h>
#endif

#define FP_META 20 /* TrafficGenerator: id, size, tos, rate, app (five u32s) */
#define FP_MAX_FLOWS 32768
#define FP_MAX_CONNS 4096
#define FP_HISTORY_NS (30ULL * NSEC_PER_SEC)

static unsigned int server_port = 5001;
#ifndef FLOWPRIO_USER_TEST
module_param(server_port, uint, 0444);
MODULE_PARM_DESC(server_port, "TrafficGenerator TCP server port");
#endif

struct fp_tuple { __be32 src, dst; __be16 sport, dport; };
struct fp_conn;
struct fp_flow {
	struct list_head all, history, active;
	struct fp_conn *conn;
	struct sk_buff_head packets;
	u64 cookie, first_ns, last_seen, wait_ns, sent, max_wait;
	u32 start, end, id, size, queued_bytes, queued_packets;
	s32 deficit;
	u32 meta_mask;
	u8 meta[FP_META], kind, band;
	bool parsed, retired;
};
struct fp_conn {
	struct hlist_node hash;
	struct list_head all, flows;
	struct fp_tuple key;
	u32 isn;
};
struct fp_sched {
	struct list_head global, flows, conns, active[2];
	DECLARE_HASHTABLE(connections, 10);
	struct sk_buff_head control;
	struct Qdisc *sch;
	u64 gc_ns, sent, fallback, crossed;
	u64 max_wait;
	u32 flow_count, conn_count, quantum, limit, per_flow;
	u32 mode, serial;
};
#ifndef FLOWPRIO_USER_TEST
static LIST_HEAD(fp_schedulers);
static DEFINE_MUTEX(fp_mutex);
#endif
static atomic64_t fp_cookie = ATOMIC64_INIT(0);

struct fp_packet_cb { u64 arrival_ns; struct fp_flow *flow; };
static struct fp_packet_cb *fp_cb(struct sk_buff *skb)
{
	qdisc_cb_private_validate(skb, sizeof(struct fp_packet_cb));
	return (struct fp_packet_cb *)qdisc_skb_cb(skb)->data;
}

static u32 fp_hash(const struct fp_tuple *key)
{
	return (__force u32)key->src ^ (__force u32)key->dst ^
		((__force u32)key->sport << 16) ^ (__force u32)key->dport;
}

static void fp_reband(struct fp_sched *q, struct fp_flow *f)
{
	/* New UNKNOWN flows share priority with MICE. Only a confirmed ELEPHANT
	 * is demoted after its independent classification timer expires.
	 */
	u8 band = q->mode == 1 ? 0 : f->kind == 2;
	if (!skb_queue_empty(&f->packets) &&
	    (list_empty(&f->active) || f->band != band))
		list_move_tail(&f->active, &q->active[band]);
	f->band = band;
}

static struct fp_flow *fp_new_flow(struct fp_sched *q, struct fp_conn *c,
				 u32 seq, u64 now)
{
	struct fp_flow *f;
	if (q->flow_count >= FP_MAX_FLOWS)
		return NULL;
	f = kzalloc(sizeof(*f), GFP_ATOMIC);
	if (!f)
		return NULL;
	f->conn = c;
	f->start = seq;
	f->cookie = atomic64_inc_return(&fp_cookie);
	f->last_seen = now;
	INIT_LIST_HEAD(&f->active);
	skb_queue_head_init(&f->packets);
	list_add_tail(&f->all, &q->flows);
	list_add(&f->history, &c->flows);
	q->flow_count++;
	return f;
}

/* Only expired, superseded and empty histories are reclaimed. A temporarily
 * empty active flow is NOT complete. Sequence wrap uses signed TCP offsets.
 */
static void fp_gc(struct fp_sched *q, u64 now)
{
	struct fp_flow *f, *tmp;
	if (now < q->gc_ns)
		return;
	q->gc_ns = now + NSEC_PER_SEC;
	list_for_each_entry_safe(f, tmp, &q->flows, all) {
		if (!f->retired || f->queued_packets ||
		    now - f->last_seen < FP_HISTORY_NS)
			continue;
		list_del(&f->all);
		list_del(&f->history);
		list_del_init(&f->active);
		q->flow_count--;
		kfree(f);
	}
}

static struct fp_conn *fp_connection(struct fp_sched *q,
			 const struct fp_tuple *key, bool syn, u32 seq)
{
	struct fp_conn *c;
	u32 hash = fp_hash(key);
	hash_for_each_possible(q->connections, c, hash, hash) {
		if (memcmp(&c->key, key, sizeof(*key)))
			continue;
		if (!syn || c->isn == seq)
			return c;
		/* A new TCP incarnation must never inherit old flow identities. */
		hash_del(&c->hash);
		break;
	}
	/* Attach before traffic starts; do not guess metadata boundaries on a
	 * connection whose SYN-ACK was missed. Such packets use fallback FIFO.
	 */
	if (!syn || q->conn_count >= FP_MAX_CONNS)
		return NULL;
	c = kzalloc(sizeof(*c), GFP_ATOMIC);
	if (!c)
		return NULL;
	c->key = *key;
	c->isn = seq;
	INIT_LIST_HEAD(&c->flows);
	list_add_tail(&c->all, &q->conns);
	hash_add(q->connections, &c->hash, hash);
	q->conn_count++;
	if (!fp_new_flow(q, c, seq + 1, ktime_get_ns()))
		return NULL;
	return c;
}

static struct fp_flow *fp_locate(struct fp_sched *q, struct fp_conn *c,
			       u32 seq, u64 now)
{
	struct fp_flow *f, *newest;
	if (list_empty(&c->flows))
		return NULL;
	newest = list_first_entry(&c->flows, struct fp_flow, history);
	if (newest->parsed && (s32)(seq - newest->end) >= 0) {
		f = fp_new_flow(q, c, newest->end, now);
		if (f) {
			newest->retired = true;
			newest->last_seen = now;
		}
		return f;
	}
	list_for_each_entry(f, &c->flows, history)
		if ((s32)(seq - f->start) >= 0 &&
		    (!f->parsed || (s32)(seq - f->end) < 0))
			return f;
	return NULL;
}

static struct fp_flow *fp_track(struct fp_sched *q, struct sk_buff *skb, u64 now)
{
	struct iphdr ip;
	struct tcphdr tcp;
	struct fp_tuple key;
	struct fp_conn *c;
	struct fp_flow *f, *owner = NULL;
	int off = skb_network_offset(skb), payload, len, i, at;
	u32 seq, pos, count;
	if (skb->protocol != htons(ETH_P_IP) ||
	    skb_copy_bits(skb, off, &ip, sizeof(ip)) || ip.version != 4 ||
	    ip.ihl < 5 || ip.protocol != IPPROTO_TCP ||
	    (ntohs(ip.frag_off) & (IP_MF | IP_OFFSET)))
		return NULL;
	off += ip.ihl * 4;
	if (skb_copy_bits(skb, off, &tcp, sizeof(tcp)) || tcp.doff < 5 ||
	    ntohs(tcp.source) != server_port)
		return NULL;
	key.src = ip.saddr; key.dst = ip.daddr;
	key.sport = tcp.source; key.dport = tcp.dest;
	seq = ntohl(tcp.seq);
	c = fp_connection(q, &key, tcp.syn, seq);
	payload = off + tcp.doff * 4;
	len = ntohs(ip.tot_len) - ip.ihl * 4 - tcp.doff * 4;
	if (!c || len <= 0 || payload + len > skb->len)
		return NULL;
	seq += tcp.syn;
	/* Usually one iteration. Crossing segments are scheduled with the first
	 * logical flow they contain (frames are indivisible), and counted.
	 */
	for (pos = 0; pos < len;) {
		f = fp_locate(q, c, seq + pos, now);
		if (!f)
			break;
		if (!owner)
			owner = f;
		else if (owner != f)
			q->crossed++;
		f->last_seen = now;
		at = (s32)(seq + pos - f->start);
		if (!f->parsed) {
			for (i = max(at, 0); i < FP_META && i - at < len - pos; i++) {
				if (!skb_copy_bits(skb, payload + pos + i - at,
						   &f->meta[i], 1))
					f->meta_mask |= 1U << i;
			}
			if (f->meta_mask == ((1U << FP_META) - 1)) {
				f->id = get_unaligned_le32(f->meta);
				f->size = get_unaligned_le32(f->meta + 4);
				/* Generator uses 32-bit size; limit intervals to signed
				 * TCP sequence space for unambiguous wrap-safe tracking.
				 */
				if (f->size > INT_MAX - FP_META)
					return NULL;
				f->end = f->start + FP_META + f->size;
				f->parsed = true;
				if (!f->id)
					f->kind = 1; /* termination response: never classify */
			}
		}
		count = f->parsed ? min_t(u32, len - pos, f->end - seq - pos)
				 : len - pos;
		if (at + count > FP_META && !f->first_ns)
			f->first_ns = now;
		if (!count)
			break;
		pos += count;
	}
	return owner;
}

static int fp_enqueue(struct sk_buff *skb, struct Qdisc *sch,
		      struct sk_buff **to_free)
{
	struct fp_sched *q = qdisc_priv(sch);
	struct fp_flow *f;
	u64 now = ktime_get_ns();
	fp_gc(q, now);
	fp_cb(skb)->arrival_ns = now;
	fp_cb(skb)->flow = NULL;
	/* Offloads are disabled by the runner. Refuse giant super-packets rather
	 * than silently pretending they are non-preemptible MTU-sized frames.
	 */
	if (skb_is_gso(skb) || qdisc_pkt_len(skb) > 65536 ||
	    sch->q.qlen >= q->limit)
		return qdisc_drop(skb, sch, to_free);
	f = fp_track(q, skb, now);
	if (f) {
		if (f->queued_packets >= q->per_flow)
			return qdisc_drop(skb, sch, to_free);
		if (!f->queued_packets) {
			f->wait_ns = now;
			f->deficit = 0;
		}
		fp_cb(skb)->flow = f;
		__skb_queue_tail(q->mode ? &f->packets : &q->control, skb);
		f->queued_packets++;
		f->queued_bytes += qdisc_pkt_len(skb);
		/* Do not move an existing queue on every enqueue: that would let a
		 * busy sender continually lose its round-robin position.
		 */
		if (q->mode && list_empty(&f->active))
			fp_reband(q, f);
	} else {
		__skb_queue_tail(&q->control, skb);
		q->fallback++;
	}
	sch->q.qlen++;
	qdisc_qstats_backlog_inc(sch, skb);
	return NET_XMIT_SUCCESS;
}

static struct sk_buff *fp_take(struct fp_sched *q, u64 now)
{
	struct fp_flow *f;
	struct sk_buff *skb;
	u32 len, band;
	int rounds = 0;
	/* FIFO baseline still tracks IDs but ignores class state. */
	if (!q->mode) {
		skb = __skb_dequeue(&q->control);
		if (!skb || !fp_cb(skb)->flow)
			return skb;
		f = fp_cb(skb)->flow;
		goto accounting;
	}
	/* Control traffic has a bounded share: one packet per five selections
	 * when both exist; when only control exists it uses the whole link.
	 */
	if (!skb_queue_empty(&q->control) &&
	    (!(q->serial++ % 5) ||
	     (list_empty(&q->active[0]) && list_empty(&q->active[1]))))
		return __skb_dequeue(&q->control);
	/* Strict priority between bands; DRR only among flows in the same band.
	 * No bandwidth reservation or aging promotion for the lower band.
	 */
	while (rounds++ < 128 * ((int)q->flow_count + 1)) {
		band = list_empty(&q->active[0]) ? 1 : 0;
		if (list_empty(&q->active[band]))
			break;
		f = list_first_entry(&q->active[band], struct fp_flow, active);
		skb = skb_peek(&f->packets);
		len = qdisc_pkt_len(skb);
		if (f->deficit < len) {
			f->deficit += q->quantum;
			list_move_tail(&f->active, &q->active[band]);
			continue;
		}
		f->deficit -= len;
		goto selected;
	}
	return __skb_dequeue(&q->control);
selected:
	skb = __skb_dequeue(&f->packets);
accounting:
	len = qdisc_pkt_len(skb);
	f->queued_packets--;
	f->queued_bytes -= len;
	f->sent += len;
	f->max_wait = max(f->max_wait, now - f->wait_ns);
	q->max_wait = max(q->max_wait, now - f->wait_ns);
	f->wait_ns = now;
	if (!q->mode)
		return skb;
	if (skb_queue_empty(&f->packets))
		list_del_init(&f->active);
	else {
		fp_reband(q, f);
		if (f->deficit < qdisc_pkt_len(skb_peek(&f->packets)))
			list_move_tail(&f->active, &q->active[f->band]);
	}
	return skb;
}

static struct sk_buff *fp_dequeue(struct Qdisc *sch)
{
	struct fp_sched *q = qdisc_priv(sch);
	struct sk_buff *skb;
	u64 now = ktime_get_ns();
	if (!sch->q.qlen)
		return NULL;
	skb = fp_take(q, now);
	if (!skb)
		return NULL;
	sch->q.qlen--;
	qdisc_qstats_backlog_dec(sch, skb);
	qdisc_bstats_update(sch, skb);
	q->sent += qdisc_pkt_len(skb);
	/* The parent HTB class owns rate limiting and burst credit.  This leaf
	 * qdisc only chooses the next logical flow and remains work-conserving.
	 */
	return skb;
}

static struct sk_buff *fp_peek(struct Qdisc *sch)
{
	return qdisc_peek_dequeued(sch);
}

static void fp_reset(struct Qdisc *sch)
{
	struct fp_sched *q = qdisc_priv(sch);
	struct fp_flow *f, *tmp;
	struct fp_conn *c, *ctmp;
	list_for_each_entry_safe(f, tmp, &q->flows, all) {
		__skb_queue_purge(&f->packets);
		list_del(&f->all); list_del(&f->history);
		list_del_init(&f->active); kfree(f);
	}
	list_for_each_entry_safe(c, ctmp, &q->conns, all) {
		list_del(&c->all); kfree(c);
	}
	hash_init(q->connections);
	__skb_queue_purge(&q->control);
	sch->q.qlen = 0; sch->qstats.backlog = 0;
	q->flow_count = q->conn_count = 0;
}

#ifndef FLOWPRIO_USER_TEST
static int fp_init(struct Qdisc *sch, struct nlattr *opt,
		   struct netlink_ext_ack *extack)
{
	struct fp_sched *q = qdisc_priv(sch);
	INIT_LIST_HEAD(&q->global);
	INIT_LIST_HEAD(&q->flows); INIT_LIST_HEAD(&q->conns);
	INIT_LIST_HEAD(&q->active[0]); INIT_LIST_HEAD(&q->active[1]);
	hash_init(q->connections); skb_queue_head_init(&q->control);
	q->sch = sch;
	q->quantum = 1514;
	q->limit = 4096; q->per_flow = 256; q->mode = 2;
	if (!net_eq(dev_net(qdisc_dev(sch)), &init_net))
		return -EOPNOTSUPP;
	mutex_lock(&fp_mutex);
	list_add_tail(&q->global, &fp_schedulers);
	mutex_unlock(&fp_mutex);
	return 0;
}

static void fp_destroy(struct Qdisc *sch)
{
	struct fp_sched *q = qdisc_priv(sch);
	mutex_lock(&fp_mutex); list_del_init(&q->global); mutex_unlock(&fp_mutex);
	fp_reset(sch);
}

static struct Qdisc_ops fp_ops __read_mostly = {
	.id = "flowprio", .priv_size = sizeof(struct fp_sched),
	.enqueue = fp_enqueue, .dequeue = fp_dequeue, .peek = fp_peek,
	.init = fp_init, .reset = fp_reset, .destroy = fp_destroy,
	.owner = THIS_MODULE,
};

static int fp_show(struct seq_file *m, void *unused)
{
	struct fp_sched *q;
	struct fp_flow *f;
	bool full = (unsigned long)m->private;
	mutex_lock(&fp_mutex);
	list_for_each_entry(q, &fp_schedulers, global) {
		spin_lock_bh(qdisc_lock(q->sch));
		/* Preserve the old boost-count columns as zero for log readers. */
		seq_printf(m, "PORT %d %u %llu %llu %llu %llu %u %u %llu\n",
			qdisc_dev(q->sch)->ifindex, q->mode, q->sent, 0ULL,
			q->fallback, q->crossed, q->sch->q.qlen,
			q->sch->qstats.drops, q->max_wait);
		list_for_each_entry(f, &q->flows, all) {
			if (!f->parsed || !f->id || !f->first_ns ||
			    (!full && f->kind))
				continue;
			seq_printf(m, "FLOW %d %llu %pI4 %u %u %llu %u %u %u %llu %llu %llu %u\n",
				qdisc_dev(q->sch)->ifindex, f->cookie,
				&f->conn->key.dst, f->id, f->size, f->first_ns,
				f->kind, f->queued_packets, f->queued_bytes,
				f->sent, 0ULL, f->max_wait, f->retired);
		}
		spin_unlock_bh(qdisc_lock(q->sch));
	}
	mutex_unlock(&fp_mutex);
	return 0;
}
static int fp_open(struct inode *inode, struct file *file)
{
	return single_open(file, fp_show, pde_data(inode));
}
static ssize_t fp_write(struct file *file, const char __user *buf,
			size_t len, loff_t *offset)
{
	char line[256], cmd[16];
	int ifindex, mode, found = -ENOENT;
	unsigned long long cookie, rate;
	u32 kind, quantum, limit, per_flow;
	struct fp_sched *q;
	struct fp_flow *f;
	if (!capable(CAP_NET_ADMIN))
		return -EPERM;
	if (!len || len >= sizeof(line) || copy_from_user(line, buf, len))
		return -EINVAL;
	line[len] = 0;
	if (sscanf(line, "%15s %d", cmd, &ifindex) != 2)
		return -EINVAL;
	mutex_lock(&fp_mutex);
	list_for_each_entry(q, &fp_schedulers, global) {
		if (qdisc_dev(q->sch)->ifindex != ifindex)
			continue;
		spin_lock_bh(qdisc_lock(q->sch));
		if (!strcmp(cmd, "class") &&
		    sscanf(line, "%*s %*d %llu %u", &cookie, &kind) == 2 &&
		    (kind == 1 || kind == 2)) {
			list_for_each_entry(f, &q->flows, all) {
				if (f->cookie != cookie)
					continue;
				if (!f->retired && f->id) {
					f->kind = kind;
					fp_reband(q, f);
					found = 0;
				}
				break;
			}
		} else if (!strcmp(cmd, "config") && !q->flow_count &&
		    sscanf(line, "%*s %*d %llu %d %u %u %u",
			&rate, &mode, &quantum, &limit, &per_flow) == 5 && rate >= 100000 &&
		    rate <= 100000000000ULL && mode >= 0 && mode <= 2 &&
		    quantum >= 1500 && quantum <= 65536 &&
		    limit && limit <= 65536 && per_flow && per_flow <= limit) {
			/* RATE remains in the control message for installer/manifest
			 * compatibility.  The parent HTB class performs shaping in v3.
			 */
			q->mode = mode; q->quantum = quantum;
			q->limit = limit; q->per_flow = per_flow;
			found = 0;
		} else {
			found = -EINVAL;
		}
		spin_unlock_bh(qdisc_lock(q->sch));
		break;
	}
	mutex_unlock(&fp_mutex);
	return found ? found : len;
}
static const struct proc_ops fp_proc_ops = {
	.proc_open = fp_open, .proc_read = seq_read, .proc_write = fp_write,
	.proc_lseek = seq_lseek, .proc_release = single_release,
};
static int __init fp_module_init(void)
{
	int err;
	if (!server_port || server_port > 65535)
		return -EINVAL;
	err = register_qdisc(&fp_ops);
	if (err)
		return err;
	if (!proc_create_data("flowprio", 0600, init_net.proc_net,
			      &fp_proc_ops, NULL))
		goto fail;
	if (!proc_create_data("flowprio_stats", 0400, init_net.proc_net,
			      &fp_proc_ops, (void *)1)) {
		remove_proc_entry("flowprio", init_net.proc_net);
		goto fail;
	}
	return 0;
fail:
	unregister_qdisc(&fp_ops);
	return -ENOMEM;
}
static void __exit fp_module_exit(void)
{
	remove_proc_entry("flowprio_stats", init_net.proc_net);
	remove_proc_entry("flowprio", init_net.proc_net);
	unregister_qdisc(&fp_ops);
}
module_init(fp_module_init);
module_exit(fp_module_exit);
MODULE_LICENSE("GPL");
MODULE_VERSION("4");
MODULE_DESCRIPTION("Work-conserving TrafficGenerator flow.id scheduler below an external shaper");
#endif

/* Userspace adapters only: scheduler and parser logic remain in the real
 * sch_flowprio.c, included directly by kernel_policy_test.c. This is not a
 * substitute for loading the module and testing actual kernel concurrency.
 */
#include <assert.h>
#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <limits.h>
#include <arpa/inet.h>
#include <linux/ip.h>
#include <linux/tcp.h>
#include <linux/if_ether.h>

typedef uint8_t u8;
typedef uint16_t u16;
typedef uint32_t u32;
typedef uint64_t u64;
typedef int32_t s32;
#define __force
#define NSEC_PER_SEC 1000000000ULL
#define NSEC_PER_MSEC 1000000ULL
#define U64_MAX UINT64_MAX
#define IP_MF 0x2000
#define IP_OFFSET 0x1fff
#define GFP_ATOMIC 0
#define NET_XMIT_SUCCESS 0
#define min_t(type,a,b) ((type)(a) < (type)(b) ? (type)(a) : (type)(b))
#define max(a,b) ((a) > (b) ? (a) : (b))
#define kzalloc(size,flags) calloc(1, size)
#define kfree free
#define container_of(ptr,type,member) ((type *)((char *)(ptr)-offsetof(type,member)))
static u64 test_now = NSEC_PER_SEC;
static u64 ktime_get_ns(void) { return test_now; }
typedef u64 atomic64_t;
#define ATOMIC64_INIT(n) (n)
static u64 atomic64_inc_return(atomic64_t *v) { return ++*v; }
static u32 get_unaligned_le32(const u8 *p)
{ return (u32)p[0] | (u32)p[1]<<8 | (u32)p[2]<<16 | (u32)p[3]<<24; }

struct list_head { struct list_head *next, *prev; };
static void INIT_LIST_HEAD(struct list_head *h) { h->next = h->prev = h; }
static bool list_empty(const struct list_head *h) { return h->next == h; }
static void list_add(struct list_head *n, struct list_head *h)
{ n->next=h->next; n->prev=h; h->next->prev=n; h->next=n; }
static void list_add_tail(struct list_head *n, struct list_head *h)
{ n->next=h; n->prev=h->prev; h->prev->next=n; h->prev=n; }
static void list_del(struct list_head *n) { n->next->prev=n->prev; n->prev->next=n->next; }
static void list_del_init(struct list_head *n) { list_del(n); INIT_LIST_HEAD(n); }
static void list_move_tail(struct list_head *n, struct list_head *h)
{ list_del(n); list_add_tail(n,h); }
#define list_entry(n,type,member) container_of(n,type,member)
#define list_first_entry(h,type,member) list_entry((h)->next,type,member)
#define list_for_each_entry(p,h,member) \
 for (p=list_entry((h)->next,__typeof__(*p),member); &(p)->member!=(h); \
      p=list_entry((p)->member.next,__typeof__(*p),member))
#define list_for_each_entry_safe(p,t,h,member) \
 for (p=list_entry((h)->next,__typeof__(*p),member), \
      t=list_entry((p)->member.next,__typeof__(*p),member); &(p)->member!=(h); \
      p=t,t=list_entry((t)->member.next,__typeof__(*t),member))

struct hlist_node { struct hlist_node *next, **pprev; };
struct hlist_head { struct hlist_node *first; };
#define DECLARE_HASHTABLE(name,bits) struct hlist_head name[1]
#define hash_init(table) ((table)[0].first = NULL)
static void test_hash_add(struct hlist_head *h, struct hlist_node *n)
{ n->next=h->first; n->pprev=&h->first; if(n->next)n->next->pprev=&n->next; h->first=n; }
#define hash_add(table,n,key) test_hash_add(&(table)[0],n)
#define hash_for_each_possible(table,p,member,key) \
 for(struct hlist_node *node=(table)[0].first; \
     node && ((p=container_of(node,__typeof__(*p),member)),1); node=node->next)
static void hash_del(struct hlist_node *n)
{ *n->pprev=n->next; if(n->next)n->next->pprev=n->pprev; }

struct qdisc_skb_cb { _Alignas(8) u8 data[24]; };
struct sk_buff {
 struct sk_buff *next;
 u8 *data;
 u32 len;
 u16 protocol;
 int tag;
 bool gso;
 struct qdisc_skb_cb cb;
};
struct sk_buff_head { struct sk_buff *first,*last; unsigned int qlen; };
static void skb_queue_head_init(struct sk_buff_head *h) { memset(h,0,sizeof(*h)); }
static bool skb_queue_empty(const struct sk_buff_head *h) { return !h->qlen; }
static unsigned int skb_queue_len(const struct sk_buff_head *h) { return h->qlen; }
static struct sk_buff *skb_peek(const struct sk_buff_head *h) { return h->first; }
static void __skb_queue_tail(struct sk_buff_head *h, struct sk_buff *s)
{ s->next=NULL; if(h->last)h->last->next=s; else h->first=s; h->last=s; h->qlen++; }
static struct sk_buff *__skb_dequeue(struct sk_buff_head *h)
{ struct sk_buff *s=h->first; if(s){h->first=s->next; if(!h->first)h->last=NULL; h->qlen--;} return s; }
static void test_free_skb(struct sk_buff *s) { free(s->data); free(s); }
static void __skb_queue_purge(struct sk_buff_head *h)
{ struct sk_buff *s; while((s=__skb_dequeue(h)))test_free_skb(s); }
static bool skb_is_gso(struct sk_buff *s) { return s->gso; }
static int skb_network_offset(struct sk_buff *s) { return 14; }
static int skb_copy_bits(struct sk_buff *s,int off,void *to,int len)
{ if(off<0 || off+len>(int)s->len)return -1; memcpy(to,s->data+off,len); return 0; }
#define qdisc_cb_private_validate(s,size) assert((size)<=sizeof((s)->cb.data))
#define qdisc_skb_cb(s) (&(s)->cb)
static u32 qdisc_pkt_len(struct sk_buff *s) { return s->len; }
struct fp_sched;
struct Qdisc { struct fp_sched *priv; struct { u32 qlen; } q;
 struct { u32 backlog, drops; } qstats; };
#define qdisc_priv(s) ((s)->priv)
static void qdisc_qstats_backlog_inc(struct Qdisc *q,struct sk_buff *s) { q->qstats.backlog+=s->len; }
static void qdisc_qstats_backlog_dec(struct Qdisc *q,struct sk_buff *s) { q->qstats.backlog-=s->len; }
static void qdisc_bstats_update(struct Qdisc *q,struct sk_buff *s) {}
static struct sk_buff *qdisc_peek_dequeued(struct Qdisc *q) { return NULL; }
static int qdisc_drop(struct sk_buff *s,struct Qdisc *q,struct sk_buff **unused)
{ q->qstats.drops++; test_free_skb(s); return 1; }

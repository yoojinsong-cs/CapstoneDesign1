#define FLOWPRIO_USER_TEST
#include "../kernel/sch_flowprio.c"

static void setup(struct fp_sched *q, struct Qdisc *sch)
{
	memset(q,0,sizeof(*q)); memset(sch,0,sizeof(*sch));
	sch->priv=q; q->sch=sch;
	INIT_LIST_HEAD(&q->flows); INIT_LIST_HEAD(&q->conns);
	INIT_LIST_HEAD(&q->active[0]); INIT_LIST_HEAD(&q->active[1]);
	hash_init(q->connections); skb_queue_head_init(&q->control);
	q->quantum=1514;
	q->limit=4096; q->per_flow=256; q->mode=2;
	test_now=NSEC_PER_SEC;
}
static struct fp_conn *connection(struct fp_sched *q, int client, u32 isn)
{
	struct fp_tuple key = { inet_addr("20.0.0.1"),
		htonl(0x14000100+client), htons(5001), htons(30000) };
	return fp_connection(q,&key,true,isn);
}
static struct sk_buff *packet(struct fp_conn *c,u32 seq,const void *body,int len,int tag)
{
	struct sk_buff *s=calloc(1,sizeof(*s));
	struct iphdr ip={0}; struct tcphdr tcp={0};
	s->len=54+len; s->data=calloc(1,s->len); s->tag=tag;
	s->protocol=htons(ETH_P_IP);
	ip.version=4; ip.ihl=5;
	ip.protocol=IPPROTO_TCP; ip.saddr=c->key.src; ip.daddr=c->key.dst;
	ip.tot_len=htons(40+len);
	tcp.doff=5; tcp.seq=htonl(seq);
	tcp.source=c->key.sport; tcp.dest=c->key.dport;
	memcpy(s->data+14,&ip,sizeof(ip)); memcpy(s->data+34,&tcp,sizeof(tcp));
	if(body)memcpy(s->data+54,body,len);
	return s;
}
static struct fp_flow *ready_flow(struct fp_sched *q,int client,int kind)
{
	struct fp_conn *c=connection(q,client,100);
	struct fp_flow *f=list_first_entry(&c->flows,struct fp_flow,history);
	f->parsed=true; f->id=client; f->size=10000000;
	f->end=f->start+FP_META+f->size; f->kind=kind;
	return f;
}
static void enqueue(struct fp_sched *q,struct fp_flow *f,int tag)
{
	struct sk_buff *unused=NULL;
	assert(fp_enqueue(packet(f->conn,f->start+FP_META,NULL,1460,tag),q->sch,&unused)==0);
	test_now++;
}
static int take(struct fp_sched *q)
{
	struct sk_buff *s=fp_take(q,test_now);
	int tag;
	assert(s); tag=s->tag; test_free_skb(s); return tag;
}
static void test_strict_priority_drains_mice_first(void)
{
	struct fp_sched q; struct Qdisc sch; int i;
	setup(&q,&sch);
	struct fp_flow *low=ready_flow(&q,1,2),*high=ready_flow(&q,2,1);
	for(i=0;i<20;i++){enqueue(&q,low,1);enqueue(&q,high,2);}
	for(i=0;i<20;i++)assert(take(&q)==2);
	assert(!high->queued_packets && low->sent==0);
	for(i=0;i<20;i++)assert(take(&q)==1);
	fp_reset(&sch);
}
static void test_strict_priority_with_different_packet_sizes(void)
{
	/* Packet sizes must never give ELEPHANT a turn while MICE is queued. */
	const int payloads[][2] = {{1460,1444}, {1444,1460}, {7,1460}, {1460,7}};
	for (unsigned int n=0;n<sizeof(payloads)/sizeof(payloads[0]);n++) {
		struct fp_sched q; struct Qdisc sch; struct sk_buff *unused=NULL;
		setup(&q,&sch); q.per_flow=2048;
		struct fp_flow *high=ready_flow(&q,1,1), *low=ready_flow(&q,2,2);
		for (int i=0;i<2000;i++) {
			assert(fp_enqueue(packet(high->conn,high->start+FP_META,NULL,payloads[n][0],1),
				&sch,&unused)==0);
			assert(fp_enqueue(packet(low->conn,low->start+FP_META,NULL,payloads[n][1],2),
				&sch,&unused)==0);
		}
		for (int i=0;i<1200;i++) take(&q);
		assert(high->queued_packets && low->queued_packets);
		assert(high->sent==1200ULL*(54+payloads[n][0]));
		assert(low->sent==0);
		fp_reset(&sch);
	}
}
static void test_new_mice_preempts_elephant(void)
{
	struct fp_sched q; struct Qdisc sch; setup(&q,&sch);
	struct fp_flow *low=ready_flow(&q,1,2), *high=ready_flow(&q,2,1);
	enqueue(&q,low,11); enqueue(&q,low,12); enqueue(&q,low,13);
	assert(take(&q)==11); /* use the whole link when MICE is absent */
	enqueue(&q,high,21); enqueue(&q,high,22);
	assert(take(&q)==21); assert(take(&q)==22);
	assert(take(&q)==12); assert(take(&q)==13);
	fp_reset(&sch);
}
static void test_fair_ignores_priority(void)
{
	struct fp_sched q; struct Qdisc sch; setup(&q,&sch); q.mode=1;
	struct fp_flow *a=ready_flow(&q,1,2), *b=ready_flow(&q,2,1);
	for (int i=0;i<100;i++) { enqueue(&q,a,1); enqueue(&q,b,2); }
	for (int i=0;i<100;i++) assert(take(&q)==i%2+1);
	assert(a->sent==b->sent);
	fp_reset(&sch);
}
static void test_change_applies_to_existing_backlog(void)
{
	struct fp_sched q; struct Qdisc sch; setup(&q,&sch);
	struct fp_flow *a=ready_flow(&q,1,0),*b=ready_flow(&q,2,0);
	enqueue(&q,a,11);enqueue(&q,a,12);enqueue(&q,b,21);enqueue(&q,b,22);
	a->kind=2;fp_reband(&q,a); /* demote the existing UNKNOWN backlog */
	assert(a->band==1 && b->band==0);
	assert(take(&q)==21);assert(take(&q)==22);
	assert(take(&q)==11);assert(take(&q)==12); /* retain every queued packet */
	fp_reset(&sch);
}
static void test_waiting_elephant_never_bypasses_mice(void)
{
	struct fp_sched q; struct Qdisc sch; setup(&q,&sch);
	struct fp_flow *low=ready_flow(&q,1,2),*high=ready_flow(&q,2,1);
	enqueue(&q,low,1);enqueue(&q,low,1);enqueue(&q,high,2);
	test_now+=30*NSEC_PER_SEC;
	assert(take(&q)==2); assert(low->sent==0);
	assert(low->kind==2 && low->band==1);
	assert(take(&q)==1); assert(take(&q)==1);
	fp_reset(&sch);
}
static void test_unknown_shares_priority_with_mice(void)
{
	struct fp_sched q; struct Qdisc sch; int i; setup(&q,&sch);
	struct fp_flow *a=ready_flow(&q,1,0),*b=ready_flow(&q,2,0);
	for(i=0;i<20;i++){enqueue(&q,a,1);enqueue(&q,b,2);}
	for(i=0;i<10;i++){assert(take(&q)==1);assert(take(&q)==2);}
	assert(a->band==0 && b->band==0);
	struct fp_flow *mice=ready_flow(&q,3,1);
	enqueue(&q,mice,3);
	assert(take(&q)==1); assert(take(&q)==2);
	assert(take(&q)==3); /* MICE and UNKNOWN use the same DRR band */
	b->kind=1;fp_reband(&q,b); /* MICE confirmation keeps its current turn */
	assert(take(&q)==1); assert(take(&q)==2);
	fp_reset(&sch);
}
static void test_new_unknown_preempts_confirmed_elephant(void)
{
	struct fp_sched q; struct Qdisc sch; setup(&q,&sch);
	struct fp_flow *low=ready_flow(&q,1,2);
	enqueue(&q,low,11);enqueue(&q,low,12);enqueue(&q,low,13);
	assert(take(&q)==11);
	struct fp_flow *fresh=ready_flow(&q,2,0);
	enqueue(&q,fresh,21);enqueue(&q,fresh,22);
	assert(take(&q)==21);assert(take(&q)==22);
	assert(take(&q)==12);assert(take(&q)==13);
	fp_reset(&sch);
}
static void test_fifo_ignores_priority(void)
{
	struct fp_sched q; struct Qdisc sch; setup(&q,&sch);q.mode=0;
	struct fp_flow *a=ready_flow(&q,1,2),*b=ready_flow(&q,2,1);
	enqueue(&q,a,11);enqueue(&q,b,21);enqueue(&q,a,12);
	assert(take(&q)==11);assert(take(&q)==21);assert(take(&q)==12);
	fp_reset(&sch);
}
static void test_work_conserving_dequeue_and_limits(void)
{
	struct fp_sched q; struct Qdisc sch; struct sk_buff *s,*unused=NULL;
	setup(&q,&sch);q.per_flow=1;
	struct fp_flow *a=ready_flow(&q,1,1);
	enqueue(&q,a,1);
	assert(fp_enqueue(packet(a->conn,a->start+FP_META,NULL,1460,2),&sch,&unused)==1);
	assert(sch.qstats.drops==1);
	s=fp_dequeue(&sch);assert(s);test_free_skb(s);
	/* The leaf scheduler must never wait on an internal pacing timer. */
	enqueue(&q,a,3);s=fp_dequeue(&sch);assert(s && s->tag==3);
	test_free_skb(s);assert(sch.q.qlen==0);assert(!sch.qstats.backlog);
	fp_reset(&sch);
}
static void test_metadata_split_reuse_retransmission_and_wrap(void)
{
	struct fp_sched q; struct Qdisc sch; u32 metadata[5]={7,100,0,0,2};
	struct sk_buff *s; struct fp_flow *f,*next;
	setup(&q,&sch);
	struct fp_conn *c=connection(&q,1,0xfffffff0);
	u32 start=c->isn+1;
	s=packet(c,start,metadata,8,0);f=fp_track(&q,s,test_now);test_free_skb(s);
	assert(f && !f->parsed);
	s=packet(c,start+FP_META,NULL,10,0);assert(fp_track(&q,s,test_now)==f);test_free_skb(s);
	assert(f->first_ns==test_now);test_now+=10;
	s=packet(c,start+8,(u8 *)metadata+8,FP_META-8,0);
	assert(fp_track(&q,s,test_now)==f);test_free_skb(s);
	assert(f->parsed && f->id==7 && f->size==100 && f->end==start+FP_META+100);
	metadata[0]=8;
	s=packet(c,f->end,metadata,FP_META,0);next=fp_track(&q,s,test_now);test_free_skb(s);
	assert(next && next!=f && next->id==8 && f->retired);
	s=packet(c,start+FP_META,NULL,10,0);assert(fp_track(&q,s,test_now)==f);test_free_skb(s);
	assert(f->first_ns==NSEC_PER_SEC);
	struct fp_conn *other=connection(&q,2,100);
	s=packet(other,101,metadata,FP_META,0);
	struct fp_flow *other_flow=fp_track(&q,s,test_now);test_free_skb(s);
	assert(other_flow && other_flow->id==8 && other_flow->cookie!=next->cookie);
	fp_reset(&sch);
}
static void test_app_field_is_header_not_flow_payload(void)
{
    struct fp_sched q; struct Qdisc sch;
    u32 metadata[5]={9,100,0,0,2}; struct sk_buff *s;
    setup(&q,&sch);
    struct fp_conn *c=connection(&q,1,100);
    u32 start=c->isn+1;
    s=packet(c,start,metadata,16,0);
    struct fp_flow *f=fp_track(&q,s,test_now);test_free_skb(s);
    assert(f && !f->parsed && !f->first_ns);
    s=packet(c,start+16,(u8 *)metadata+16,4,0);
    assert(fp_track(&q,s,test_now)==f);test_free_skb(s);
    assert(f->parsed && f->end==start+20+100 && !f->first_ns);
    test_now+=50;
    s=packet(c,start+20,NULL,1,0);
    assert(fp_track(&q,s,test_now)==f);test_free_skb(s);
    assert(f->first_ns==test_now);
    fp_reset(&sch);
}
int main(void)
{
	test_strict_priority_drains_mice_first();
	test_strict_priority_with_different_packet_sizes();
	test_new_mice_preempts_elephant();
	test_fair_ignores_priority();
	test_change_applies_to_existing_backlog();
	test_waiting_elephant_never_bypasses_mice();
	test_unknown_shares_priority_with_mice();
	test_new_unknown_preempts_confirmed_elephant();
	test_fifo_ignores_priority();
	test_work_conserving_dequeue_and_limits();
	test_metadata_split_reuse_retransmission_and_wrap();
	test_app_field_is_header_not_flow_payload();
	puts("12 actual kernel-source policy/parser tests passed");
	return 0;
}

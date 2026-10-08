#include "conn.h"
#include "common.h"
#include <stdio.h>
#include <string.h>
#include <unistd.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <arpa/inet.h>
#include <errno.h>
#include <sys/time.h>

static void close_conn_socket(struct conn_node *node)
{
    if (node && node->sockfd >= 0)
    {
        shutdown(node->sockfd, SHUT_RDWR);
        close(node->sockfd);
        node->sockfd = -1;
    }
}

/* initialize connection */
bool init_conn_node(struct conn_node *node, int id, struct conn_list *list)
{
    struct sockaddr_in serv_addr;
    int sock_opt = 1;
    struct timeval timeout = {TG_SOCKET_TIMEOUT_SEC, 0};

    if (!node)
        return false;

    node->id = id;
    node->busy = false;
    node->next = NULL;
    node->list = list;
    node->connected = false;
    node->thread_started = false;
    node->sockfd = -1;

    memset(&serv_addr, 0, sizeof(serv_addr));
    serv_addr.sin_family = AF_INET;
    serv_addr.sin_addr.s_addr = inet_addr(list->ip);
    serv_addr.sin_port = htons(list->port);

    /* initialize server socket */
    node->sockfd = socket(AF_INET, SOCK_STREAM, 0);
    if (node->sockfd < 0)
    {
        char msg[256] = {0};
        snprintf(msg, 256, "Error: init socket (to %s:%hu) in init_conn_node()", list->ip, list->port);
        perror(msg);
        close_conn_socket(node);
        return false;
    }

    /* set socket options */
    if (setsockopt(node->sockfd, SOL_SOCKET, SO_REUSEADDR, &sock_opt, sizeof(sock_opt)) < 0)
    {
        char msg[256] = {0};
        snprintf(msg, 256, "Error: set SO_REUSEADDR (to %s:%hu) in init_conn_node()", list->ip, list->port);
        perror(msg);
        close_conn_socket(node);
        return false;
    }
    if (setsockopt(node->sockfd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout)) < 0 ||
        setsockopt(node->sockfd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout)) < 0)
    {
        char msg[256] = {0};
        snprintf(msg, 256, "Error: set socket timeout (to %s:%hu) in init_conn_node()", list->ip, list->port);
        perror(msg);
        close_conn_socket(node);
        return false;
    }
    if (setsockopt(node->sockfd, IPPROTO_TCP, TCP_NODELAY, &sock_opt, sizeof(sock_opt)) < 0)
    {
        char msg[256] = {0};
        snprintf(msg, 256, "Error: set TCP_NODELAY (to %s:%hu) in init_conn_node()", list->ip, list->port);
        perror(msg);
        close_conn_socket(node);
        return false;
    }

    if (connect(node->sockfd, (struct sockaddr *) &serv_addr, sizeof(serv_addr)) < 0)
    {
        char msg[256] = {0};
        snprintf(msg, 256, "Error: connect() (to %s:%hu) in init_conn_node()", list->ip, list->port);
        perror(msg);
        return false;
    }

    node->connected = true;
    return true;
}

bool init_conn_list(struct conn_list *list, int index, char *ip, unsigned short port)
{
    if (!list)
        return false;

    if (strlen(ip) < sizeof(list->ip))
    {
        strcpy(list->ip, ip);
        list->ip[strlen(list->ip)] = '\0';
    }
    else
        return false;

    list->index = index;
    list->port = port;
    list->head = NULL;
    list->tail = NULL;
    list->len = 0;
    list->available_len = 0;
    list->flow_finished = 0;
    pthread_mutex_init(&(list->lock), NULL);

    return true;
}

/* insert several nodes to the tail of the linked list */
bool insert_conn_list(struct conn_list *list, int num)
{
    int i = 0;
    struct conn_node *new_node = NULL;

    if (!list)
        return false;

    for (i = 0; i < num; i++)
    {
        new_node = (struct conn_node*)malloc(sizeof(struct conn_node));
        if (!new_node)
        {
            perror("Error: malloc connection node");
            return false;
        }
        if (!init_conn_node(new_node, list->len, list))
        {
            free(new_node);
            return false;
        }

        /* if the list is empty */
        if (list->len == 0)
        {
            list->head = new_node;
            list->tail = new_node;
        }
        else
        {
            list->tail->next = new_node;
            list->tail = new_node;
        }
        pthread_mutex_lock(&(list->lock));
        list->len++;
        list->available_len++;
        pthread_mutex_unlock(&(list->lock));
    }

    return true;
}

/* search the first available connection (busy==false && connected==true) in the list. */
struct conn_node *search_conn_list(struct conn_list *list)
{
    struct conn_node *ptr = NULL;

    if (!list || !(list->available_len))
        return NULL;

    ptr = list->head;
    while (true)
    {
        if (!ptr)
            return NULL;
        else if (!(ptr->busy) && ptr->connected)
            return ptr;
        else
            ptr = ptr->next;
    }

    return NULL;
}

struct conn_node *acquire_conn_list(struct conn_list *list)
{
    struct conn_node *ptr = NULL;

    if (!list)
        return NULL;

    pthread_mutex_lock(&(list->lock));
    for (ptr = list->head; ptr != NULL; ptr = ptr->next)
    {
        if (!ptr->busy && ptr->connected && ptr->thread_started)
        {
            ptr->busy = true;
            if (list->available_len > 0)
                list->available_len--;
            pthread_mutex_unlock(&(list->lock));
            return ptr;
        }
    }
    pthread_mutex_unlock(&(list->lock));
    return NULL;
}

unsigned int connected_conn_count(struct conn_list *list)
{
    struct conn_node *ptr = NULL;
    unsigned int count = 0;

    if (!list)
        return 0;

    pthread_mutex_lock(&(list->lock));
    for (ptr = list->head; ptr != NULL; ptr = ptr->next)
    {
        if (ptr->connected)
            count++;
    }
    pthread_mutex_unlock(&(list->lock));
    return count;
}

bool start_conn_thread(struct conn_node *node, void *(*start_routine)(void *))
{
    pthread_attr_t attr;
    int rc;

    if (!node || !start_routine)
        return false;

    rc = pthread_attr_init(&attr);
    if (rc == 0)
    {
        rc = pthread_attr_setstacksize(&attr, TG_THREAD_STACK_SIZE);
        if (rc == 0)
            rc = pthread_create(&(node->thread), &attr, start_routine, (void*)node);
        pthread_attr_destroy(&attr);
    }

    if (rc == 0)
    {
        node->thread_started = true;
        return true;
    }

    fprintf(stderr, "Error: cannot create listener thread for %s:%hu: %s\n",
            node->list->ip, node->list->port, strerror(rc));
    pthread_mutex_lock(&(node->list->lock));
    if (!node->busy && node->list->available_len > 0)
        node->list->available_len--;
    node->busy = false;
    node->connected = false;
    pthread_mutex_unlock(&(node->list->lock));
    close_conn_socket(node);
    return false;
}

/* search N available connections in the list */
struct conn_node **search_n_conn_list(struct conn_list *list, unsigned int num)
{
    struct conn_node *ptr = NULL;
    struct conn_node **result = NULL;
    int i = 0;

    if (!list || list->available_len < num || !num)
        return NULL;

    result = (struct conn_node**)malloc(num * sizeof(struct conn_node*));
    if (!result)
    {
        perror("Error: malloc result in search_n_conn_list()");
        return NULL;
    }

    ptr = list->head;
    while (true)
    {
        if (ptr)
        {
            if (!(ptr->busy) && ptr->connected)
                result[i++] = ptr;

            if (i < num)
                ptr = ptr->next;
            else
                return result;
        }
        else
        {
            free(result);
            return NULL;
        }
    }

    return NULL;
}

/* wait for all threads in the linked list to finish */
void wait_conn_list(struct conn_list *list)
{
    struct conn_node *ptr = NULL;
    struct timespec ts;
    int s;

    if (!list)
        return;

    ptr = list->head;
    while (true)
    {
        if (!ptr)
            break;
        else
        {
            if (!ptr->thread_started)
            {
                ptr = ptr->next;
                continue;
            }
            /* if this connection is active, the socket timeout bounds the wait */
            if (ptr->connected)
            {
                s = pthread_join(ptr->thread, NULL);
                if (s != 0)
                {
                    char msg[256] = {0};
                    snprintf(msg, 256, "Error: pthread_join() (to %s:%hu) in wait_conn_list()",
                             list->ip, list->port);
                    fprintf(stderr, "%s: %s\n", msg, strerror(s));
                }
            }
            else
            {
                clock_gettime(CLOCK_REALTIME, &ts);
                ts.tv_sec += 5;
                s = pthread_timedjoin_np(ptr->thread, NULL, &ts);
                if (s != 0)
                {
                    char msg[256] = {0};
                    snprintf(msg, 256, "Error: pthread_timedjoin_np() (to %s:%hu) in wait_conn_list()",
                             list->ip, list->port);
                    if (s != ETIMEDOUT)
                        fprintf(stderr, "%s: %s\n", msg, strerror(s));
                }
            }
            ptr = ptr->next;
        }
    }
}

/* clear all the nodes in the linked list */
void clear_conn_list(struct conn_list *list)
{
    struct conn_node *ptr = NULL;
    struct conn_node *next_node = NULL;

    if (!list)
        return;

    for (ptr = list->head; ptr != NULL; ptr = next_node)
    {
        next_node = ptr->next;
        free(ptr);
    }

    list->len = 0;
}

/* print information of the linked list */
void print_conn_list(struct conn_list *list)
{
    if (list)
        printf("%s:%hu  total connections: %u  available connections: %u  flows finished: %u\n",
               list->ip, list->port, list->len, list->available_len, list->flow_finished);
}

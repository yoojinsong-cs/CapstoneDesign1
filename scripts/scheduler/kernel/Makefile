obj-m += sch_flowprio.o
KDIR ?= /lib/modules/$(shell uname -r)/build
KCC ?= gcc-12
all:
	$(MAKE) -C $(KDIR) M=$(CURDIR) CC=$(KCC) modules
clean:
	$(MAKE) -C $(KDIR) M=$(CURDIR) clean

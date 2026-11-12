# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""datacenter_visualization's own example layouts, and what its parser makes.

The ``.dc`` reader in :mod:`meridian_commander.dclayout` is a second parser of
a format whose parser is dcviz's JavaScript, so it is held to that one's
answers on dcviz's own example files (``examples/*.dc``, dcviz 1.57.0), with
the comment lines taken out.  For each: the text, the number of elements, how
many of each kind, and a SHA-256 of every element's path, kind, U-slot, size,
rack height, name, attributes (inherited included) and tags, one line each,
as dcviz's ``parseLayout`` gave them -- the lines ``digest`` in
``test_dclayout.py`` builds from this reader's elements.  They are kept as
Python so that they travel with the tests in the source distribution.
"""

EXAMPLES = {
    "small": (
        """\
dc IAD1 name="Acme IAD1" +prod region=us-east tier=3
  room DH1 name="Data Hall 1" cols=2 +hall-a
    row A..D +compute
      rack R[01..06] u=42 +standard
        node tor at=42 role=tor +switch model=sn3700
        node u[01..20] role=server +x86 +gpu model=hgx-h100 cpu=epyc-9654
    row E..F +storage
      rack R[01..04] u=42
        node tor at=42 role=tor +switch
        node d[01..10] u=4 role=server +storage model=jbod-90 disks=90
  room DH2 name="Data Hall 2" +hall-b
    row A..B +compute
      rack R[01..08]
        node tor at=42 role=tor +switch
        node u[01..30] role=server +x86 model=r7625
  room MDF name="Meet-Me Room" +network
    row S1
      rack SP[1..4]
        node spine at=40 u=2 role=spine +switch +core
net data    label="Data / east-west"  color=#4fa3ff
net mgmt    label="Out-of-band mgmt"  color=#8b93a7 style=dashed
net storage label="Storage fabric"    color=#ff9f43
link data    role=server role=tor   scope=rack
link mgmt    role=server role=tor   scope=rack
link storage +storage,role=server scope=row mode=mesh
link data    role=tor    role=spine
""",
        1157, {'dc': 1, 'room': 3, 'row': 9, 'rack': 52, 'node': 1092},
        "2b59f7fbd8ea5ea34aa37a9bd908c2183d0f52c37f6717781fbb42e484e5eac8"),
    "three-rows": (
        """\
dc DC1 name="Data Center 1"
  room ROOM1 name="Room 1"
    row A..C
      rack R[1..4,7..10] u=42
        node [7..15x2,25..31x2] id=u{id} at={id} role=server
""",
        245, {'dc': 1, 'room': 1, 'row': 3, 'rack': 24, 'node': 216},
        "a000d2a8b39b07d0e18c175025929c403b6d222c670a7ff3ef6e1344117dd58b"),
    "hostnames": (
        """\
dc FLAT name="Flat-named DC" cols=2
  room wr[01..04] +prod
    row A
      rack r[01..06] u=42
        node tor at=42 role=tor +switch name={room}{rack}tor
        node u[01..40] role=server name={room}{rack}{id} +x86
    row B
      rack r[07..12] u=42
        node tor at=42 role=tor +switch name={room}{rack}tor
        node u[01..40] role=server name={room}{rack}{id} +x86
    row C
      rack r[13..18] u=42
        node tor at=42 role=tor +switch name={room}{rack}tor
        node u[01..40] role=server name={room}{rack}{id} +x86
net data label="Data" color=#4fa3ff
net mgmt label="Mgmt" color=#8b93a7 style=dashed
link data role=server role=tor scope=rack
link mgmt role=server role=tor scope=rack
""",
        3041, {'dc': 1, 'room': 4, 'row': 12, 'rack': 72, 'node': 2952},
        "56a5594d4a5a28b8e2c6e431b3e73f8373d41020222d5ddabaa47b01973a260a"),
    "dual-plane": (
        """\
dc LAB name="Dual-plane fabric" cols=1
  room CORE name="Superspine planes" cols=2 +network
    plane PL1 name="Plane 1" plane=1
      rack SS1 u=12
        node ss[1..6] u=2 role=superspine +switch
    plane PL2 name="Plane 2" plane=2
      rack SS2 u=12
        node ss[1..6] u=2 role=superspine +switch
  room DH1 name="Data Hall 1" cols=2
    pod [1..2] id=P{id} name="Pod {id}"
      row A
        rack [1..2] id=R{seq:2} u=24
          node tora at=24 role=tor nic=a +switch
          node torb at=23 role=tor nic=b +switch
          node s[1..8] u=2 role=server +x86
      row S
        rack S id=SP{seq} u=8
          node spine1 at=6 u=2 role=spine plane=1 +switch
          node spine2 at=2 u=2 role=spine plane=2 +switch
net nica    label="NIC a"              color=#4fa3ff  gbps=25
net nicb    label="NIC b"              color=#ff9f43  gbps=25
net peer    label="TOR a - TOR b"      color=#f5d442  gbps=100
net fabric  label="TOR to spine"       color=#4dd4ac  gbps=100
net plane1  label="Plane 1"            color=#c986ff  gbps=100
net plane2  label="Plane 2"            color=#ff6b8b  gbps=100
link nica   role=server role=tor,nic=a  scope=rack splice=4
link nicb   role=server role=tor,nic=b  scope=rack splice=4
link peer   role=tor,nic=a role=tor,nic=b  scope=rack
link fabric role=tor role=spine  scope=pod
link plane1 role=spine,plane=1 role=superspine,plane=1
link plane1 role=spine,plane=1 role=superspine,plane=1
link plane1 role=spine,plane=1 role=superspine,plane=1
link plane1 role=spine,plane=1 role=superspine,plane=1
link plane2 role=spine,plane=2 role=superspine,plane=2
link plane2 role=spine,plane=2 role=superspine,plane=2
link plane2 role=spine,plane=2 role=superspine,plane=2
link plane2 role=spine,plane=2 role=superspine,plane=2
""",
        75, {'dc': 1, 'room': 2, 'plane': 2, 'rack': 8, 'node': 56, 'pod': 2, 'row': 4},
        "5c3dcbd3cfb40aa3c98595f1ef187cb09d7bc5d629572cf30e0165b1180ba823"),
    "mega": (
        """\
dc MEGA name="Hyperscale MEGA-1" +prod region=us-west cols=3
  hall H[1..8] name="Hall {id}" cols=1 +hall power=grid-{i}
    row C[01..16] +compute +x86
      rack R[01..40] u=42
        node tor at=42 role=tor +switch model=sn5600
        node u[01..40] role=server model=gen11 cpu=turin cores=192
    row G[01..05] +accel +gpu
      rack R[01..40] u=42
        node tor at=42 role=tor +switch model=sn5600
        node u[01..10] u=4 role=server model=hgx-b200 gpus=8
    row S[01..03] +storage
      rack R[01..40] u=42
        node tor at=42 role=tor +switch
        node d[01..20] u=2 role=server model=jbod-102 disks=102
    row SP name="Spines" +network
      rack SP[1..4]
        node spine[1..2] u=2 role=spine +switch
  hall MMR name="Meet-Me Room" +network
    row CORE
      rack C[1..4]
        node core[1..2] u=2 role=core +switch
net data    label="Data / east-west" color=#4fa3ff
net mgmt    label="Out-of-band"      color=#8b93a7 style=dashed
net storage label="Storage fabric"   color=#ff9f43
link data    role=server role=tor   scope=rack
link mgmt    role=server role=tor   scope=rack
link data    role=tor    role=spine scope=hall
link data    role=spine  role=core
link storage +storage,role=server   scope=row mode=chain
""",
        255679, {'dc': 1, 'hall': 9, 'row': 201, 'rack': 7716, 'node': 247752},
        "b76f40d49b0ef12e6a7c2d6e16be211678b2b2406efdf41c00f1c8d2b6aee2ac"),
}

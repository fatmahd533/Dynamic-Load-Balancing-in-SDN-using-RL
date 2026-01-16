from mininet.topo import Topo
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.cli import CLI
from mininet.log import setLogLevel
import time


class FatTree4(Topo):
    def build(self):
        print("🔨 Construction de la topologie FatTree4...")
        
        # Core layer
        c1 = self.addSwitch('s5')
        c2 = self.addSwitch('s6')

        # Aggregation / edge switches
        s1 = self.addSwitch('s1')
        s2 = self.addSwitch('s2')
        s3 = self.addSwitch('s3')
        s4 = self.addSwitch('s4')

        # Links core <-> aggregation
        self.addLink(c1, s1)
        self.addLink(c1, s2)
        self.addLink(c2, s3)
        self.addLink(c2, s4)

        # Inter-aggregation links
        self.addLink(s1, s3)
        self.addLink(s2, s4)

        # Hosts
        for i in range(1, 7):
            h = self.addHost(f'h{i}')
            if i <= 2:
                self.addLink(h, s1)
            elif i <= 4:
                self.addLink(h, s2)
            elif i == 5:
                self.addLink(h, s3)
            elif i == 6:
                self.addLink(h, s4)


def enable_stp(net):
    """Active STP sur tous les switches et attend la convergence"""
    print("\n🔧 Activation de STP (Spanning Tree Protocol)...")
    
    for switch in net.switches:
        # Activer STP sur le switch
        switch.cmd('ovs-vsctl set Bridge', switch, 'stp_enable=true')
        print(f"  ✅ STP activé sur {switch.name}")
    
    print("⏳ Attente de la convergence STP (35 secondes)...")
    for i in range(35, 0, -1):
        print(f"   {i} secondes restantes...", end='\r')
        time.sleep(1)
    print("\n")
    
    # Vérifier l'état STP
    print("📊 État STP des switches:")
    for switch in net.switches:
        result = switch.cmd('ovs-vsctl get Bridge', switch, 'stp_enable')
        stp_status = "activé" in result or "true" in result
        status_icon = "✅" if stp_status else "❌"
        print(f"  {status_icon} {switch.name}: STP {result.strip()}")


def run():
    print("\n=== Lancement de Mininet avec topologie FatTree4 ===")
    print("📞 Connexion au contrôleur Ryu sur 127.0.0.1:6633...\n")

    topo = FatTree4()

    net = Mininet(
        topo=topo,
        controller=lambda name: RemoteController(name, ip='127.0.0.1', port=6633),
        switch=OVSSwitch,
        autoSetMacs=True,
        autoStaticArp=True
    )

    net.start()
    
    print("➡️ Mininet est lancé.")
    
    # Vérification des connexions des switches
    print("\n🔍 Vérification des connexions des switches...")
    for switch in net.switches:
        print(f"  ✅ Switch {switch.name} démarré")
    
    # Étape 1: Attendre que le contrôleur Ryu installe les règles initiales
    print("\n⏳ Attente de l'installation des règles par Ryu (15 secondes)...")
    time.sleep(15)
    
    # Étape 2: Activer STP et attendre la convergence
    enable_stp(net)
    
    # Étape 3: Test de connectivité
    print("\n🔍 Test de connectivité après convergence STP...")
    net.pingAll()
    
    # Étape 4: Tests manuels supplémentaires si nécessaire
    print("\n🔄 Tests manuels des connexions critiques...")
    critical_tests = [
        ('h1', 'h2'),  # Même switch
        ('h1', 'h3'),  # Switches différents
        ('h1', 'h5'),  # À travers le réseau
        ('h1', 'h6'),  # Chemin le plus long
        ('h2', 'h4'),  # Autre chemin
        ('h3', 'h6'),  # Combinaison différente
    ]
    
    success_count = 0
    for src, dst in critical_tests:
        print(f"  Test {src} -> {dst}: ", end="")
        try:
            result = net.ping(hosts=[net.get(src), net.get(dst)], timeout=2)
            if result == 0:
                print("✅ Réussi")
                success_count += 1
            else:
                print("❌ Échec")
        except:
            print("❌ Erreur")
    
    print(f"\n📈 Résumé: {success_count}/{len(critical_tests)} tests critiques réussis")
    
    if success_count == len(critical_tests):
        print("🎉 Tous les tests de connectivité sont réussis!")
    else:
        print("⚠️  Certaines connexions ont échoué, vérifiez la configuration")
    
    print("\n💡 Commandes utiles dans CLI:")
    print("   pingall          # Test complet de connectivité")
    print("   links            # Vérifier l'état des liens")
    print("   nodes            # Lister tous les nœuds")
    print("   net              # Voir la topologie complète")
    
    CLI(net)
    net.stop()
    print("\n🛑 Mininet arrêté.")


if __name__ == '__main__':
    setLogLevel('info')
    run()

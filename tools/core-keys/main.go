// core-keys prints the installer keys a core trusts, installersig.Keys, one
// standard base64 line each. It belongs to no module of this repository: the
// release runs it inside the pinned core, scripts/check-core-keys.sh.
package main

import (
	"encoding/base64"
	"fmt"

	"github.com/ReCasaOS/CasaOS/pkg/installersig"
)

func main() {
	for _, k := range installersig.Keys {
		fmt.Println(base64.StdEncoding.EncodeToString(k))
	}
}

locals {
  # The input alphabet contains no regex metacharacters. Match path segments,
  # so /api matches /api and /api/items, but never /apix or /api-v2.
  path_patterns = { for key, route in var.routes :
    key => route.path_prefix == "/" ? "^/" : "^${route.path_prefix}(/|$)"
  }

  # Octavia renumbers policy positions. Exclude all same-host descendants from
  # their parent policy, making longest-prefix selection independent of order.
  # ponytail: at most N*(N-1)/2 exclusions (1225 at N=50); this bounded map
  # avoids a new ordering controller. Check the site's L7 rule quota before apply.
  path_exclusions = { for pair in setproduct(keys(var.routes), keys(var.routes)) :
    "${pair[0]}:${pair[1]}" => { parent = pair[0], child = pair[1] }
    if pair[0] != pair[1] && var.routes[pair[0]].host == var.routes[pair[1]].host && (
      var.routes[pair[0]].path_prefix == "/" ||
      startswith(var.routes[pair[1]].path_prefix, "${var.routes[pair[0]].path_prefix}/")
    )
  }
}

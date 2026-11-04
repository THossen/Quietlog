import { StyleSheet, Text, View } from 'react-native';
import { colors } from '../theme';

export default function Footer() {
  return (
    <View style={styles.bar}>
      <Text style={styles.text}>
        Quietlog records what you tell it. It does not give medical advice. If this feels like an
        emergency, contact your local emergency services.
      </Text>
    </View>
  );
}

const styles = StyleSheet.create({
  bar: {
    marginTop: 8,
    marginHorizontal: 4,
    paddingHorizontal: 16,
    paddingVertical: 16,
    borderTopWidth: 1,
    borderTopColor: colors.edge,
  },
  text: { color: colors.dim, fontSize: 13, lineHeight: 19, textAlign: 'center', opacity: 0.86 },
});

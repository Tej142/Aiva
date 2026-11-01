package com.example.facttv;

import android.os.Bundle;

import androidx.activity.EdgeToEdge;
import androidx.appcompat.app.AppCompatActivity;
import androidx.core.graphics.Insets;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowInsetsCompat;
import android.view.View;
import android.widget.*;

public class MainActivity extends AppCompatActivity {

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);
        Button btn = findViewById(R.id.btn);

        TextView tv = findViewById(R.id.tv);

        btn.setOnClickListener(new View.OnClickListener(){
            public void onClick(View v){
                EditText et = findViewById(R.id.et);
                String s = et.getText().toString();
                int num = Integer.parseInt(s);
                long i,fact=1;

                for(i=1;i<=num;i++){
                    fact = fact*i;
                }

                tv.setText(String.valueOf(fact));
            }
        });
    }
}